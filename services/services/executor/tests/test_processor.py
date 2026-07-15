"""processor 测试 —— mock httpx + repository，覆盖所有分支。

关键场景：
  - 2xx → succeeded + mark_succeeded
  - 4xx / 5xx → failed + mark_failed(error_code=backend_http_XXX)
  - timeout → mark_failed(error_code=backend_timeout)
  - connection error → mark_failed(error_code=backend_unreachable)
  - 已是 running → mark_running 返回 False → 跳过
"""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from apihub_core.events import TaskRequest


def make_msg(**overrides) -> TaskRequest:
    base = {
        "task_id": "task_abc123",
        "api_id": "api_users",
        "api_version_id": "ver_1",
        "backend_url": "http://backend.internal/users",
        "payload": '{"user":"u1"}',
        "timeout_seconds": 5.0,
        "tenant_id": "t1",
        "app_id": "app_x",
        "request_id": "req_1",
        "trace_id": "tr_1",
    }
    base.update(overrides)
    return TaskRequest(**base)


def make_response(status_code=200, text="{}"):
    r = MagicMock()
    r.status_code = status_code
    r.text = text
    return r


@pytest.fixture
def mocks(monkeypatch):
    """spy 所有副作用，便于断言调用次数和参数。"""
    from executor import processor as p

    calls = {"running": [], "succeeded": [], "failed": [], "emit": []}

    async def _mark_running(task_id):
        calls["running"].append(task_id)
        return p._mark_running_ret if hasattr(p, "_mark_running_ret") else True

    async def _mark_succeeded(task_id, response_body, http_status):
        calls["succeeded"].append((task_id, response_body, http_status))

    async def _mark_failed(task_id, error_code, error_msg, http_status=None):
        calls["failed"].append((task_id, error_code, error_msg, http_status))

    async def _emit(topic, payload, key=None, extra_headers=None):
        calls["emit"].append((topic, payload, key, extra_headers))

    async def _emit_event(event):
        calls["emit"].append(event)

    monkeypatch.setattr(p.repo, "mark_running", _mark_running)
    monkeypatch.setattr(p.repo, "mark_succeeded", _mark_succeeded)
    monkeypatch.setattr(p.repo, "mark_failed", _mark_failed)
    monkeypatch.setattr(p.kafka, "emit", _emit)
    monkeypatch.setattr(p.kafka, "emit_event", _emit_event)

    return calls


@pytest.fixture(autouse=True)
def http_client(monkeypatch):
    """注入一个 mock httpx client，避免真实网络。"""
    from executor import processor as p

    fake = MagicMock()
    fake.post = AsyncMock()
    monkeypatch.setattr(p, "_client", fake)
    return fake


class TestSuccess:
    async def test_2xx_marks_succeeded(self, http_client, mocks):
        http_client.post.return_value = make_response(200, '{"ok":1}')

        from executor.processor import process_task

        result = await process_task(make_msg())

        assert result.status == "succeeded"
        assert result.http_status == 200
        assert result.response_body == '{"ok":1}'

        # mark_succeeded 收到 body + status
        assert mocks["succeeded"] == [("task_abc123", '{"ok":1}', 200)]
        # mark_failed 不应被调
        assert mocks["failed"] == []
        # 推了 task-status 事件（typed：emit_event 收到 TaskStatus）
        assert len(mocks["emit"]) == 1
        assert mocks["emit"][0].TOPIC == "task-status"
        assert mocks["emit"][0].status == "succeeded"

    async def test_201_also_succeeded(self, http_client, mocks):
        http_client.post.return_value = make_response(201, "created")

        from executor.processor import process_task

        result = await process_task(make_msg())

        assert result.status == "succeeded"
        assert result.http_status == 201


class TestHttpErrors:
    async def test_4xx_marks_failed_with_status(self, http_client, mocks):
        http_client.post.return_value = make_response(404, "not found")

        from executor.processor import process_task

        result = await process_task(make_msg())

        assert result.status == "failed"
        assert result.http_status == 404
        assert result.error_code == "backend_http_404"
        assert mocks["failed"][0][:3] == ("task_abc123", "backend_http_404", "not found")

    async def test_500_marks_failed(self, http_client, mocks):
        http_client.post.return_value = make_response(500, "boom")

        from executor.processor import process_task

        result = await process_task(make_msg())

        assert result.status == "failed"
        assert result.error_code == "backend_http_500"

    async def test_long_error_body_truncated(self, http_client, mocks):
        http_client.post.return_value = make_response(500, "x" * 10000)

        from executor.processor import process_task

        await process_task(make_msg())

        # mark_failed 收到的 error_msg 截断到 500
        _, _, err_msg, _ = mocks["failed"][0]
        assert len(err_msg) == 500


class TestTimeout:
    async def test_timeout_marks_timeout(self, http_client, mocks):
        import asyncio

        async def _slow(*args, **kwargs):
            await asyncio.sleep(10)

        http_client.post.side_effect = httpx.TimeoutException("read timeout")

        from executor.processor import process_task

        result = await process_task(make_msg(timeout_seconds=0.05))

        assert result.status == "timeout"
        assert result.error_code == "backend_timeout"
        assert mocks["failed"][0][1] == "backend_timeout"


class TestConnectionError:
    async def test_connection_refused(self, http_client, mocks):
        http_client.post.side_effect = httpx.ConnectError("conn refused")

        from executor.processor import process_task

        result = await process_task(make_msg())

        assert result.status == "failed"
        assert result.error_code == "backend_unreachable"
        assert "ConnectError" in result.error_msg


class TestIdempotency:
    async def test_skipped_when_already_running(self, http_client, mocks, monkeypatch):
        """mark_running 返回 False → 跳过，不打 backend。"""
        from executor import processor as p

        # 强制 mark_running 返回 False
        async def _false(task_id):
            return False

        monkeypatch.setattr(p.repo, "mark_running", _false)

        result = await p.process_task(make_msg())

        assert result.status == "skipped"
        http_client.post.assert_not_called()
        assert mocks["succeeded"] == []
        assert mocks["failed"] == []


class TestHeaders:
    async def test_request_headers_set(self, http_client, mocks):
        """调 backend 时应带 X-Task-Id / X-Tenant-Id 等。"""
        http_client.post.return_value = make_response(200, "{}")

        from executor.processor import process_task

        await process_task(make_msg())

        args, kwargs = http_client.post.call_args
        assert kwargs["headers"]["X-Task-Id"] == "task_abc123"
        assert kwargs["headers"]["X-Tenant-Id"] == "t1"
        assert kwargs["headers"]["X-Request-Id"] == "req_1"
        assert kwargs["headers"]["X-Trace-Id"] == "tr_1"

    async def test_payload_passed_through(self, http_client, mocks):
        http_client.post.return_value = make_response(200, "{}")

        from executor.processor import process_task

        await process_task(make_msg(payload='{"k":"v"}'))

        _, kwargs = http_client.post.call_args
        assert kwargs["content"] == b'{"k":"v"}'


async def test_call_backend_forwards_w3c_traceparent(monkeypatch):
    """活跃 span 下，_call_backend 给 backend 的请求头必须含 W3C traceparent。"""
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    # 装一个真 tracer provider，让 propagate.inject 能写出 traceparent
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(trace, "get_tracer", lambda *a, **kw: provider.get_tracer(*a, **kw))

    from executor import processor

    # 构造真实 httpx client 前清掉宿主机代理 env（httpx 会读 ALL_PROXY 等；
    # 本测试立刻 monkeypatch post，不走真实网络，无需代理）。
    for _k in ("ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(_k, raising=False)
    await processor.init_http_client()

    captured: dict = {}

    class _FakeResp:
        status_code = 200
        text = '{"ok": true}'

    async def _fake_post(url, content=None, headers=None, timeout=None):  # noqa: ASYNC109
        captured["headers"] = headers or {}
        return _FakeResp()

    monkeypatch.setattr(processor._client, "post", _fake_post)

    msg = TaskRequest(
        task_id="task_t1xx",
        api_id="a1",
        api_version_id="v1",
        backend_url="http://mock/echo",
        payload="",
        timeout_seconds=5.0,
        tenant_id="tenant_a",
        app_id="app_trading",
        request_id="r1",
        trace_id="abc",
    )

    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("parent-span"):
        result = await processor._call_backend(msg)

    assert result.status == "succeeded"
    assert "traceparent" in captured["headers"], captured["headers"]
    # traceparent 格式：00-<32 hex trace>-<16 hex span>-<2 flags>
    tp = captured["headers"]["traceparent"]
    assert tp.startswith("00-") and len(tp.split("-")) == 4, tp
    await processor.close_http_client()
