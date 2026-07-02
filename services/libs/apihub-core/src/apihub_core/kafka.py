"""Kafka 事件投递 — 调用事件 / 任务 / 审计 / 通知。

消息强制带 tenant_id header（便于下游路由 / 配额统计）。
详见 docs/04-data-model.md §6 Kafka topic 规范。
"""

import json
from typing import Any, Optional

import aiokafka

from apihub_core.config import Settings
from apihub_core.tenant import get_tenant_context

_producer: Optional[aiokafka.AIOKafkaProducer] = None


async def init_producer(settings: Settings) -> None:
    global _producer
    _producer = aiokafka.AIOKafkaProducer(
        bootstrap_servers=settings.kafka_brokers,
        value_serializer=lambda v: json.dumps(v).encode("utf-8"),
        key_serializer=lambda k: k.encode("utf-8") if k else None,
        acks="all",
        enable_idempotence=True,
        compression_type="lz4",
        linger_ms=10,
    )
    await _producer.start()


async def close_producer() -> None:
    global _producer
    if _producer:
        await _producer.stop()
        _producer = None


async def emit(
    topic: str,
    payload: dict[str, Any],
    key: Optional[str] = None,
    extra_headers: Optional[dict[str, str]] = None,
) -> None:
    """投递事件。租户上下文自动注入 header。"""
    if _producer is None:
        raise RuntimeError("Kafka producer not initialized")

    headers = list(extra_headers.items()) if extra_headers else []
    ctx = get_tenant_context()
    if ctx:
        headers.append(("tenant_id", ctx.tenant_id))
        headers.append(("tenant_type", ctx.tenant_type))
        if ctx.app_id:
            headers.append(("app_id", ctx.app_id))
        # 用 tenant_id 作分区 key（同租户消息顺序一致）
        if key is None:
            key = ctx.tenant_id

    await _producer.send_and_wait(topic, payload, key=key, headers=headers)
