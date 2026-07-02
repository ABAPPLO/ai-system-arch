"""repository 测试 —— mock admin_db_session 验证 SQL 状态机。

不接真 PG（那是 integration test 的活）。这里只验证：
  - mark_running 用对的 SQL（条件 status='pending'）
  - mark_succeeded / mark_failed 字段对
  - mark_failed 把 'timeout' error_code 映射到 status='timeout'
  - reset_stale_running SQL 模板能跑
"""


import pytest


class _FakeConn:
    """模拟 asyncpg.Connection，记所有 execute/fetchrow。"""

    def __init__(self, fetchrow_ret=None, execute_ret="UPDATE 1"):
        self.fetchrow_ret = fetchrow_ret
        self.execute_ret = execute_ret
        self.calls = []

    async def fetchrow(self, sql, *args):
        self.calls.append(("fetchrow", sql, args))
        return self.fetchrow_ret

    async def execute(self, sql, *args):
        self.calls.append(("execute", sql, args))
        return self.execute_ret


@pytest.fixture
def patch_db(monkeypatch):
    """让 admin_db_session yield 一个 fake conn。"""
    from contextlib import asynccontextmanager

    from apihub_core import db as db_mod

    fake_conn = _FakeConn()

    @asynccontextmanager
    async def _fake_session():
        yield fake_conn

    monkeypatch.setattr(db_mod, "admin_db_session", _fake_session)
    return fake_conn


class TestGetTaskStatus:
    async def test_returns_status_when_found(self, patch_db):
        patch_db.fetchrow_ret = {"status": "pending"}

        from executor.repository import get_task_status
        result = await get_task_status("task_1")

        assert result == "pending"
        assert "SELECT status FROM task WHERE id = $1" in patch_db.calls[0][1]

    async def test_returns_none_when_missing(self, patch_db):
        patch_db.fetchrow_ret = None

        from executor.repository import get_task_status
        result = await get_task_status("task_missing")
        assert result is None


class TestMarkRunning:
    async def test_returns_true_when_pending(self, patch_db):
        patch_db.execute_ret = "UPDATE 1"

        from executor.repository import mark_running
        result = await mark_running("task_1")

        assert result is True
        sql = patch_db.calls[0][1]
        assert "status = 'running'" in sql
        assert "status = 'pending'" in sql

    async def test_returns_false_when_already_running(self, patch_db):
        """UPDATE 0 = 没抢到。"""
        patch_db.execute_ret = "UPDATE 0"

        from executor.repository import mark_running
        result = await mark_running("task_1")
        assert result is False


class TestMarkSucceeded:
    async def test_updates_response_body_and_status(self, patch_db):
        from executor.repository import mark_succeeded
        await mark_succeeded("task_1", response_body='{"ok":1}', http_status=200)

        kind, sql, args = patch_db.calls[0]
        assert kind == "execute"
        assert "status = 'succeeded'" in sql
        assert "response_body = $2" in sql
        assert "response_status = $3" in sql
        assert args == ("task_1", '{"ok":1}', 200)


class TestMarkFailed:
    async def test_normal_failure(self, patch_db):
        from executor.repository import mark_failed
        await mark_failed(
            "task_1",
            error_code="backend_http_500",
            error_msg="boom",
            http_status=500,
        )

        _, sql, args = patch_db.calls[0]
        assert "ELSE 'failed'" in sql   # CASE 走 else 分支
        assert args == ("task_1", "backend_http_500", "boom", 500)

    async def test_timeout_maps_to_timeout_status(self, patch_db):
        from executor.repository import mark_failed
        await mark_failed(
            "task_1",
            error_code="timeout",
            error_msg="read timeout",
            http_status=None,
        )

        _, sql, args = patch_db.calls[0]
        assert "WHEN $2 = 'timeout' THEN 'timeout'" in sql
        assert args == ("task_1", "timeout", "read timeout", None)


class TestResetStale:
    async def test_returns_count(self, patch_db):
        patch_db.execute_ret = "UPDATE 3"

        from executor.repository import reset_stale_running
        n = await reset_stale_running(timeout_seconds=600)

        assert n == 3
        sql = patch_db.calls[0][1]
        assert "status = 'running'" in sql
        assert "started_at < NOW()" in sql

    async def test_handles_garbage_result(self, patch_db):
        patch_db.execute_ret = "garbage"

        from executor.repository import reset_stale_running
        n = await reset_stale_running(timeout_seconds=600)
        assert n == 0
