"""consumer 测试 —— Kafka 失败消息 → PG retry_task + ZSet。"""

import pytest


class _FakeKafkaMsg:
    def __init__(self, value, key=None, headers=None):
        self.value = value
        self.key = key
        self.headers = headers or []


@pytest.fixture
def captured_creates(monkeypatch):
    """spy create_retry_task + schedule。"""
    creates = []
    schedules = []

    async def _create(**kwargs):
        creates.append(kwargs)
        return 12345

    async def _schedule(**kwargs):
        schedules.append(kwargs)

    from retry_svc import consumer as cmod
    from retry_svc import delay_queue
    from retry_svc import repository as repo

    monkeypatch.setattr(repo, "create_retry_task", _create)
    monkeypatch.setattr(cmod, "delay_queue", delay_queue)
    monkeypatch.setattr(delay_queue, "schedule", _schedule)
    return creates, schedules


class TestHandle:
    async def test_parses_payload_and_schedules(self, fake_settings, captured_creates):
        """Kafka payload + header → create_retry_task + schedule ZSet。"""
        from retry_svc.consumer import FailureConsumer

        creates, schedules = captured_creates
        c = FailureConsumer(fake_settings)

        msg = _FakeKafkaMsg(
            value={
                "task_id": "task_abcdef12",
                "tenant_id": "42",
                "api_id": "100",
                "app_id": "200",
                "trace_id": "tr_abc",
                "backend_url": "http://b/h",
                "payload": '{"x":1}',
                "error_code": "backend_http_500",
                "error_msg": "Internal Server Error",
                "max_attempts": 3,
                "backoff_base_ms": 1000,
            },
            key="42:12345",
            headers=[
                (b"tenant_id", b"42"),
                (b"app_id", b"200"),
                (b"request_id", b"req_1"),
            ],
        )
        await c._handle(msg)

        assert len(creates) == 1
        call = creates[0]
        assert call["tenant_id"] == "42"
        assert call["trace_id"] == "tr_abc"
        assert call["api_id"] == "100"
        assert call["app_id"] == "200"
        assert call["max_attempts"] == 3
        assert call["error_code"] == "backend_http_500"

        assert len(schedules) == 1
        assert schedules[0]["tenant_id"] == "42"
        assert schedules[0]["retry_task_id"] == 12345

    async def test_missing_tenant_id_skipped(self, fake_settings, captured_creates):
        """没有 tenant_id 不能 schedule（写 PG 也会失败，干脆跳过）。"""
        from retry_svc.consumer import FailureConsumer

        creates, schedules = captured_creates
        c = FailureConsumer(fake_settings)

        msg = _FakeKafkaMsg(
            value={
                "task_id": "task_abcdef12",
                "api_id": "100",
                "backend_url": "http://b/h",
                "error_code": "x",
            },
        )
        await c._handle(msg)

        assert creates == []
        assert schedules == []

    async def test_invalid_tenant_id_skipped(self, fake_settings, captured_creates):
        """空字符串 / 缺失 tenant_id 必须跳过（避免写出无主租户的脏数据）。"""
        from retry_svc.consumer import FailureConsumer

        creates, schedules = captured_creates
        c = FailureConsumer(fake_settings)

        msg = _FakeKafkaMsg(
            value={
                "task_id": "task_abcdef12",
                "tenant_id": "   ",
                "api_id": "100",
                "backend_url": "http://b/h",
                "error_code": "x",
            },
        )
        await c._handle(msg)

        assert creates == []
        assert schedules == []

    async def test_create_retry_task_failure_suppressed(
        self, fake_settings, captured_creates, monkeypatch
    ):
        """create_retry_task 抛异常 → _handle 不应炸（caller 会 commit）。"""
        from retry_svc import consumer as cmod
        from retry_svc import repository as repo

        async def _boom(**kwargs):
            raise RuntimeError("db down")

        monkeypatch.setattr(repo, "create_retry_task", _boom)
        creates, schedules = captured_creates

        c = cmod.FailureConsumer(fake_settings)
        msg = _FakeKafkaMsg(
            value={
                "task_id": "task_abcdef12",
                "tenant_id": "42",
                "api_id": "100",
                "backend_url": "http://b/h",
                "error_code": "x",
            },
            headers=[(b"tenant_id", b"42")],
        )
        await c._handle(msg)  # 不抛
        assert schedules == []
