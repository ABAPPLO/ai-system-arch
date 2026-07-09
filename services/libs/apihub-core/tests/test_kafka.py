"""Kafka emit / consume 测试 —— tenant header + W3C traceparent 注入 / 提取。"""

import pytest
from apihub_core import kafka as kafka_mod
from apihub_core.tenant import set_tenant_context


class _FakeProducer:
    def __init__(self):
        self.calls: list[dict] = []

    async def send_and_wait(self, topic, payload, key=None, headers=None):
        # 生产者把 header value 编码成 bytes（aiokafka 0.14+ 要求，见 kafka.py:94-97）；
        # 这里 decode 回 str，与消费端 extract_trace_context 语义一致，便于断言。
        decoded = {}
        for k, v in (headers or []):
            decoded[k] = v.decode("utf-8") if isinstance(v, bytes) else v
        self.calls.append({
            "topic": topic,
            "payload": payload,
            "key": key,
            "headers": decoded,
        })


@pytest.fixture
def fake_producer(monkeypatch):
    fake = _FakeProducer()
    monkeypatch.setattr(kafka_mod, "_producer", fake)
    return fake


class TestTenantHeaderInjection:
    async def test_emit_injects_tenant_id(self, fake_producer, tenant_a):
        set_tenant_context(tenant_a)
        await kafka_mod.emit("api-call-events", {"path": "/v1/foo"})

        assert len(fake_producer.calls) == 1
        call = fake_producer.calls[0]
        assert call["headers"]["tenant_id"] == "tenant_a"
        assert call["headers"]["tenant_type"] == "internal"
        assert call["headers"]["app_id"] == "app_trading"

    async def test_emit_uses_tenant_id_as_partition_key(self, fake_producer, tenant_a):
        """同租户消息进同一分区，保证顺序。"""
        set_tenant_context(tenant_a)
        await kafka_mod.emit("api-call-events", {"x": 1})

        assert fake_producer.calls[0]["key"] == "tenant_a"

    async def test_emit_explicit_key_overrides_tenant(self, fake_producer, tenant_a):
        set_tenant_context(tenant_a)
        await kafka_mod.emit("task-requests", {"x": 1}, key="task_123")

        assert fake_producer.calls[0]["key"] == "task_123"

    async def test_emit_preserves_extra_headers(self, fake_producer, tenant_a):
        set_tenant_context(tenant_a)
        await kafka_mod.emit(
            "api-call-events",
            {"x": 1},
            extra_headers={"trace_id": "trc_001", "request_id": "req_001"},
        )

        headers = fake_producer.calls[0]["headers"]
        assert headers["trace_id"] == "trc_001"
        assert headers["request_id"] == "req_001"
        # 租户 header 依然注入
        assert headers["tenant_id"] == "tenant_a"

    async def test_emit_no_tenant_no_headers(self, fake_producer):
        """没租户上下文（运维事件等）—— 不注入 tenant header，key 为 None。"""
        await kafka_mod.emit("audit-events", {"action": "system.start"})

        call = fake_producer.calls[0]
        assert call["key"] is None
        # headers 应为空 dict
        assert call["headers"] == {}


class TestPayloadPassThrough:
    async def test_payload_unchanged(self, fake_producer, tenant_a):
        set_tenant_context(tenant_a)
        payload = {"action": "api.create", "api_id": "api_001"}
        await kafka_mod.emit("audit-events", payload)

        assert fake_producer.calls[0]["payload"] == payload

    async def test_topic_routed_correctly(self, fake_producer, tenant_a):
        set_tenant_context(tenant_a)
        await kafka_mod.emit("notification", {"msg": "hello"})

        assert fake_producer.calls[0]["topic"] == "notification"


class TestInitialization:
    async def test_raises_when_not_initialized(self, monkeypatch):
        monkeypatch.setattr(kafka_mod, "_producer", None)
        with pytest.raises(RuntimeError, match="Kafka producer not initialized"):
            await kafka_mod.emit("topic", {"x": 1})


# ============ W3C traceparent 注入 / 提取 ============


@pytest.fixture(scope="module")
def otel_setup():
    """模块级 OTel provider：只能 set 一次，所以放 module 级别。

    返回 (tracer, exporter)。exporter.finished 收集本模块所有 span，
    每个测试自己 clear。
    """
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    try:
        trace.set_tracer_provider(provider)
    except Exception:
        # 已被其它模块 set 过 —— add processor 到现有 provider
        existing = trace.get_tracer_provider()
        if hasattr(existing, "add_span_processor"):
            existing.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = trace.get_tracer(__name__)
    yield tracer, exporter


class TestTracePropagation:
    """W3C traceparent：emit 时注入、consume 时提取。"""

    async def test_emit_injects_traceparent_when_span_active(
        self, fake_producer, otel_setup,
    ):
        """emit 时若有活跃 OTel span，traceparent 应写入 Kafka headers。"""
        tracer, exporter = otel_setup
        exporter.finished.clear()

        with tracer.start_as_current_span("parent_span"):
            await kafka_mod.emit("task-requests", {"x": 1})

        headers = fake_producer.calls[0]["headers"]
        # traceparent 格式：00-<32hex>-<16hex>-<2hex flags>
        tp = headers.get("traceparent", "")
        assert tp.startswith("00-"), f"expected W3C format, got {tp!r}"
        parts = tp.split("-")
        assert len(parts) == 4
        assert len(parts[1]) == 32  # trace_id
        assert len(parts[2]) == 16   # span_id
        assert len(parts[3]) == 2    # flags

        # producer span 也起了一条
        span_names = [s.name for s in exporter.finished]
        assert any("kafka.produce" in n for n in span_names)

    async def test_emit_no_active_span_no_traceparent(self, fake_producer):
        """无活跃 span → traceparent 不写入（不影响功能）。"""
        await kafka_mod.emit("audit-events", {"x": 1})
        headers = fake_producer.calls[0]["headers"]
        assert "traceparent" not in headers

    async def test_extract_trace_context_decodes_bytes(self):
        """headers 列表里 bytes 类型 → str dict。"""
        raw = [
            ("traceparent", b"00-abc-def-01"),
            ("tenant_id", b"42"),
        ]
        out = kafka_mod.extract_trace_context(raw)
        assert out["traceparent"] == "00-abc-def-01"
        assert out["tenant_id"] == "42"

    async def test_extract_trace_context_empty(self):
        assert kafka_mod.extract_trace_context(None) == {}
        assert kafka_mod.extract_trace_context([]) == {}

    async def test_consume_with_trace_propagates_to_processor(
        self, fake_producer, otel_setup,
    ):
        """端到端：producer emit → consumer 处理时，processor 内拿到的
        current span 的 trace_id 应等于 producer 的 trace_id。"""
        tracer, exporter = otel_setup
        exporter.finished.clear()

        # 1) Producer：在 parent span 内 emit
        with tracer.start_as_current_span("svc_a.parent") as parent:
            await kafka_mod.emit("task-requests", {"x": 1})
            producer_tp = fake_producer.calls[0]["headers"]["traceparent"]
            producer_trace_id = producer_tp.split("-")[1]
            parent_trace_id = format(parent.get_span_context().trace_id, "032x")
            assert producer_trace_id == parent_trace_id

        # 2) Consumer：构造 fake msg，headers 里带 traceparent
        class _Msg:
            topic = "task-requests"
            key = b"42"
            headers = [
                ("traceparent", producer_tp.encode("utf-8")),
                ("tenant_id", b"42"),
            ]
            offset = 7
            partition = 0
            value = {"x": 1}

        captured = {}

        async def _processor(msg):
            from opentelemetry import trace as _trace
            cur = _trace.get_current_span()
            captured["trace_id"] = format(
                cur.get_span_context().trace_id, "032x"
            )

        await kafka_mod.consume_with_trace(
            topic="task-requests", msg=_Msg(), processor=_processor,
        )

        # Consumer 的 trace_id 应与 producer 一致（贯穿同一条 trace）
        assert captured["trace_id"] == producer_trace_id

        # 也校验 exporter 里有 producer + consumer 两条 span
        names = [s.name for s in exporter.finished]
        assert any("kafka.produce" in n for n in names)
        assert any("kafka.consume" in n for n in names)


# InMemorySpanExporter —— 简化版，避免依赖 SDK 自带版本的 API 差异
class InMemorySpanExporter:
    """收集所有 finished span，便于测试断言。"""

    def __init__(self):
        self.finished = []

    def export(self, spans):
        self.finished.extend(spans)
        return True

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis=30000):
        return True
