"""Kafka 事件投递 — 调用事件 / 任务 / 审计 / 通知。

消息强制带 tenant_id header（便于下游路由 / 配额统计）。
W3C traceparent 自动注入 / 提取（aiokafka 不在 OTel 自动 instrumentation 列表，
需手动 propagate）。
详见 docs/04-data-model.md §6 Kafka topic 规范、docs/08-observability-security.md §5。
"""

import json
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import aiokafka
from opentelemetry import trace
from opentelemetry.context import attach, detach
from opentelemetry.propagators import composite
from opentelemetry.trace import Link, SpanKind
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from apihub_core.config import Settings
from apihub_core.tenant import get_tenant_context

_producer: aiokafka.AIOKafkaProducer | None = None

# W3C traceparent + Baggage（OTel 默认 composite，显式列出便于测试）
_PROPAGATOR = composite.CompositePropagator([
    TraceContextTextMapPropagator(),
])


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
    key: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> None:
    """投递事件。租户上下文 + W3C traceparent 自动注入 header。"""
    if _producer is None:
        raise RuntimeError("Kafka producer not initialized")

    headers: dict[str, str] = dict(extra_headers) if extra_headers else {}

    # W3C traceparent：从当前 OTel context 注入（无活跃 span 时为空，不影响）
    carrier: dict[str, str] = {}
    _PROPAGATOR.inject(carrier)
    for k, v in carrier.items():
        headers[k] = v

    # 租户 header
    ctx = get_tenant_context()
    if ctx:
        headers["tenant_id"] = ctx.tenant_id
        headers["tenant_type"] = ctx.tenant_type
        if ctx.app_id:
            headers["app_id"] = ctx.app_id
        # 用 tenant_id 作分区 key（同租户消息顺序一致）
        if key is None:
            key = ctx.tenant_id

    # PRODUCER span —— 让 Jaeger 上看到「服务 A 发出 Kafka 消息」这一跳
    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span(
        f"kafka.produce {topic}",
        kind=SpanKind.PRODUCER,
        attributes={
            "messaging.system": "kafka",
            "messaging.destination.name": topic,
            "messaging.destination.partition.id": str(key) if key else "",
        },
    ):
        # aiokafka 0.14+ 要求 header value 是 bytes（旧版自动 encode 现已报 TypeError）
        raw_headers = [
            (k, v.encode("utf-8") if isinstance(v, str) else v)
            for k, v in headers.items()
        ]
        await _producer.send_and_wait(topic, payload, key=key, headers=raw_headers)


def extract_trace_context(headers: list[tuple[str, bytes]] | dict[str, str] | None) -> dict[str, str]:
    """从 Kafka message headers 还原 W3C carrier。

    消费端用：先把 carrier 还原出来，再 propagate.extract(carrier) 拿到 context。
    返回纯 str→str 的 dict（已 decode bytes）。
    """
    if not headers:
        return {}
    if isinstance(headers, dict):
        return dict(headers)
    out: dict[str, str] = {}
    for k, v in headers:
        ks = k.decode("utf-8") if isinstance(k, bytes) else k
        vs = v.decode("utf-8") if isinstance(v, bytes) else v
        out[ks] = vs
    return out


@asynccontextmanager
async def consume_span(
    *,
    topic: str,
    msg_key: str | None = None,
    msg_headers: list[tuple[str, bytes]] | dict[str, str] | None,
    msg_offset: int | None = None,
    msg_partition: int | None = None,
):
    """消费端 span 上下文管理器。

    1. 从 headers 提取 W3C traceparent → 还原 producer context
    2. 起 CONSUMER span（kind=CONSUMER），parent = producer span
    3. attach context，让后续业务逻辑里的子 span 链接到这条 trace

    用法：
        async with kafka.consume_span(topic="task-requests", msg_headers=msg.headers, ...):
            await process_task(msg)
    """
    carrier = extract_trace_context(msg_headers)
    parent_ctx = _PROPAGATOR.extract(carrier)

    tracer = trace.get_tracer(__name__)
    attrs: dict[str, Any] = {
        "messaging.system": "kafka",
        "messaging.destination.name": topic,
    }
    if msg_partition is not None:
        attrs["messaging.destination.partition.id"] = str(msg_partition)
    if msg_offset is not None:
        attrs["messaging.kafka.offset"] = msg_offset
    if msg_key:
        attrs["messaging.kafka.message.key"] = str(msg_key)

    # 若提取到 producer context，把它作为 link（CONSUMER span 的 parent 走 remote parent）
    links = []
    parent_span_ctx = trace.get_current_span(parent_ctx).get_span_context()
    if parent_span_ctx.is_valid:
        links.append(Link(parent_span_ctx))

    with tracer.start_as_current_span(
        f"kafka.consume {topic}",
        context=parent_ctx,
        kind=SpanKind.CONSUMER,
        attributes=attrs,
        links=links,
    ) as span:
        # 把当前 span context attach 到 runtime，使下游业务 span 自动成为 child
        token = attach(trace.set_span_in_context(span))
        try:
            yield span
        finally:
            detach(token)


async def consume_with_trace(
    *,
    topic: str,
    msg,
    processor: Callable[[Any], Awaitable[Any]],
) -> Any:
    """便捷封装：从 aiokafka ConsumerRecord 提字段 → 起 consume span → 跑 processor。

    msg 需有 .headers（必需），.topic/.key/.offset/.partition 可选（aiokafka 标准字段，
    测试 fake 可能缺）。
    """
    msg_topic = getattr(msg, "topic", None) or topic
    raw_key = getattr(msg, "key", None)
    if isinstance(raw_key, bytes):
        raw_key = raw_key.decode("utf-8")
    async with consume_span(
        topic=msg_topic,
        msg_key=raw_key,
        msg_headers=msg.headers,
        msg_offset=getattr(msg, "offset", None),
        msg_partition=getattr(msg, "partition", None),
    ):
        return await processor(msg)
