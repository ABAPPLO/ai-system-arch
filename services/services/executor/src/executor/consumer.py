"""Kafka 消费循环 —— task-requests 消费组 executor。

设计要点：
  - 单消费组多 partition → 横向扩展靠副本数 + partition 数
  - 每条消息处理完才 commit（at-least-once）
  - 消息处理失败不抛 → 写 PG failed 即可，下次重投靠 retry 服务（不在本服务）
  - 头里取 tenant_id / request_id 等，回填到 TaskMessage
"""

import asyncio
import contextlib
import json

import aiokafka
from apihub_core import kafka as core_kafka
from apihub_core.config import Settings
from apihub_core.logging import get_logger

from executor.models import TaskMessage
from executor.processor import process_task

log = get_logger(__name__)

CONSUMER_GROUP = "executor"
TOPIC = "task-requests"


class TaskConsumer:
    """封装 aiokafka 消费循环，便于测试 mock。"""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._consumer: aiokafka.AIOKafkaConsumer | None = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        """初始化 consumer 并起后台 worker task。"""
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
        self._task = asyncio.create_task(self._run(), name="executor-consumer")
        log.info(
            "consumer_started",
            topic=TOPIC,
            group=CONSUMER_GROUP,
            brokers=self._settings.kafka_brokers,
        )

    async def stop(self) -> None:
        """优雅关闭：发停止信号 → 等 worker 处理完当前消息 → 关 consumer。"""
        self._stop.set()
        if self._task:
            await asyncio.wait_for(self._task, timeout=30.0)
        if self._consumer:
            await self._consumer.stop()
        log.info("consumer_stopped")

    async def _run(self) -> None:
        """主循环。任何异常都打个 log 继续拽下一条。"""
        assert self._consumer is not None
        try:
            async for msg in self._consumer:
                try:
                    # 包在 consume_span 里 → Jaeger 上能看到 producer→consumer 的链路
                    await core_kafka.consume_with_trace(
                        topic=TOPIC, msg=msg, processor=self._handle,
                    )
                    await self._consumer.commit()
                except Exception as e:
                    # 单条消息处理异常不能拖垮整个 worker
                    log.exception("task_handle_failed", error=str(e))
                    # 也 commit：失败的已经写 PG failed，再投也不会重跑（幂等也会跳）
                    with contextlib.suppress(Exception):
                        await self._consumer.commit()
                if self._stop.is_set():
                    break
        except Exception as e:
            log.exception("consumer_loop_crashed", error=str(e))

    async def _handle(self, msg) -> None:
        """把 Kafka 消息转成 TaskMessage，调 processor。"""
        payload = msg.value or {}
        headers = core_kafka.extract_trace_context(msg.headers)

        task_msg = TaskMessage(
            task_id=payload["task_id"],
            api_id=payload.get("api_id", ""),
            api_version_id=payload.get("api_version_id", ""),
            backend_url=payload["backend_url"],
            payload=payload.get("payload", ""),
            timeout_seconds=float(payload.get("timeout_seconds", 30.0)),
            callback_url=payload.get("callback_url"),
            tenant_id=headers.get("tenant_id"),
            app_id=headers.get("app_id"),
            request_id=headers.get("request_id") or payload.get("request_id"),
            trace_id=headers.get("trace_id"),
        )

        await process_task(task_msg)
