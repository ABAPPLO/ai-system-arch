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
from executor.models import TaskMessage


def make_msg(**overrides) -> TaskMessage:
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
    return TaskMessage(**base)


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

    monkeypatch.setattr(p.repo, "mark_running", _mark_running)
    monkeypatch.setattr(p.repo, "mark_succeeded", _mark_succeeded)
    monkeypatch.setattr(p.repo, "mark_failed", _mark_failed)
    monkeypatch.setattr(p.kafka, "emit", _emit)

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
        # 推了 task-status 事件
        assert len(mocks["emit"]) == 1
        assert mocks["emit"][0][0] == "task-status"
        assert mocks["emit"][0][1]["status"] == "succeeded"

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
