"""Kafka 消费循环 —— task-failures 消费组 retry。

设计要点：
  - executor 检测到 task failed → emit 到 task-failures
  - retry-svc 消费 → 创建 retry_task → 推到 Redis ZSet 延迟队列
  - 单消费组多 partition → 副本数 = 消费并行度
  - 单条消息异常不杀 worker（commit + log）
  - payload 走 typed 契约（parse_event → TaskFailure）；tenant_id 仍 header 优先
"""

import asyncio
import contextlib
import json

import aiokafka
from apihub_core import kafka as core_kafka
from apihub_core.config import Settings
from apihub_core.logging import get_logger
from apihub_core.tenant import TenantContext, set_tenant_context

from retry_svc import delay_queue
from retry_svc import repository as repo
from retry_svc.backoff import next_attempt_delay_ms
from retry_svc.models import BackoffPolicy

log = get_logger(__name__)

CONSUMER_GROUP = "retry"
TOPIC = "task-failures"


class FailureConsumer:
    """封装 aiokafka 消费循环。"""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._consumer: aiokafka.AIOKafkaConsumer | None = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self._consumer = aiokafka.AIOKafkaConsumer(
            TOPIC,
            bootstrap_servers=self._settings.kafka_brokers,
            group_id=CONSUMER_GROUP,
            enable_auto_commit=False,
            auto_offset_reset="earliest",
            value_deserializer=lambda v: json.loads(v.decode("utf-8")) if v else None,
            key_deserializer=lambda k: k.decode("utf-8") if k else None,
        )
        await self._consumer.start()
        self._task = asyncio.create_task(self._run(), name="retry-consumer")
        log.info(
            "consumer_started",
            topic=TOPIC,
            group=CONSUMER_GROUP,
            brokers=self._settings.kafka_brokers,
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await asyncio.wait_for(self._task, timeout=30.0)
        if self._consumer:
            await self._consumer.stop()
        log.info("consumer_stopped")

    async def _run(self) -> None:
        assert self._consumer is not None
        try:
            async for msg in self._consumer:
                try:
                    # 包在 consume_span 里 → Jaeger 上能看到 producer→consumer 的链路
                    await core_kafka.consume_with_trace(
                        topic=TOPIC,
                        msg=msg,
                        processor=self._handle,
                    )
                    await self._consumer.commit()
                except Exception as e:
                    log.exception("failure_handle_error", error=str(e))
                    with contextlib.suppress(Exception):
                        await self._consumer.commit()
                if self._stop.is_set():
                    break
        except Exception as e:
            log.exception("consumer_loop_crashed", error=str(e))

    async def _handle(self, msg) -> None:
        """Kafka 消息 → TaskFailure → 写 PG → 推 ZSet。"""
        payload = msg.value or {}
        headers = core_kafka.extract_trace_context(msg.headers)

        # tenant_id 优先从 header，否则从 payload
        tenant_id = headers.get("tenant_id") or payload.get("tenant_id")
        if not tenant_id:
            log.warning("failure_msg_missing_tenant", payload=payload)
            return

        if not isinstance(tenant_id, str) or not tenant_id.strip():
            log.warning("failure_msg_invalid_tenant", tenant_id=tenant_id)
            return

        # tenant_id 已校验；并入 payload 再 parse，保证 TaskFailure.tenant_id 必填有值，
        # 且与原 FailureMessage(tenant_id=tenant_id) 行为一致（header tenant 为准）。
        # app_id 原 payload 优先回落 header —— 一并并入。
        parse_payload = {
            **payload,
            "tenant_id": tenant_id,
            "app_id": payload.get("app_id") or headers.get("app_id"),
        }
        failure = core_kafka.parse_event(TOPIC, parse_payload)

        # 设 TenantContext（虽然写走 admin_db_session，但 log / OTel 需要 tenant）
        set_tenant_context(
            TenantContext(
                tenant_id=tenant_id,
                tenant_type="internal",
                app_id=failure.app_id or "",
            )
        )

        # 计算 next_retry_at
        delay_ms = next_attempt_delay_ms(
            0,
            policy=BackoffPolicy.EXPONENTIAL,
            base_ms=failure.backoff_base_ms,
        )
        # 转 unix ts（秒，浮点）
        import time

        next_ts = time.time() + delay_ms / 1000.0
        from datetime import UTC, datetime, timedelta

        next_retry_at = datetime.now(UTC) + timedelta(milliseconds=delay_ms)

        try:
            retry_task_id = await repo.create_retry_task(
                tenant_id=tenant_id,
                trace_id=failure.trace_id,
                api_id=failure.api_id,
                app_id=failure.app_id or "",
                task_instance_id=failure.task_id,
                original_request={
                    "task_id": failure.task_id,
                    "backend_url": failure.backend_url,
                    "payload": failure.payload,
                    "api_version_id": failure.api_version_id,
                    "request_id": failure.request_id,
                },
                error_code=failure.error_code,
                error_msg=failure.error_msg,
                max_attempts=failure.max_attempts,
                backoff_policy=BackoffPolicy.EXPONENTIAL,
                backoff_base_ms=failure.backoff_base_ms,
                next_retry_at=next_retry_at.replace(tzinfo=None),
                env=self._settings.env,
            )
        except Exception as e:
            log.exception("create_retry_task_failed", error=str(e))
            return

        await delay_queue.schedule(
            tenant_id=tenant_id,
            retry_task_id=retry_task_id,
            next_attempt_at_ts=next_ts,
        )

        log.info(
            "failure_scheduled",
            retry_task_id=retry_task_id,
            tenant_id=tenant_id,
            task_id=failure.task_id,
            next_retry_in_ms=delay_ms,
        )
