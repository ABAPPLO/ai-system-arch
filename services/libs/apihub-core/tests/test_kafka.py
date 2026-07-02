"""Kafka emit 测试 —— tenant_id 自动注入 header + 用作分区 key。"""

import pytest

from apihub_core import kafka as kafka_mod
from apihub_core.tenant import set_tenant_context


class _FakeProducer:
    def __init__(self):
        self.calls: list[dict] = []

    async def send_and_wait(self, topic, payload, key=None, headers=None):
        self.calls.append({
            "topic": topic,
            "payload": payload,
            "key": key,
            "headers": dict(headers or []),
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
