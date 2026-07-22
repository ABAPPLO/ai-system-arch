"""数据清理测试。"""

import contextlib
from datetime import datetime

from admin import repository as repo_mod


class _FakeConn:
    def __init__(self):
        self.executed = []

    async def fetch(self, sql, *args):
        self.executed.append(("fetch", sql[:100]))
        if "pg_class" in sql:
            return [
                {
                    "partition_name": "task_instance_2025_06",
                    "bound_expr": "FOR VALUES FROM ('2025-06-01') TO ('2025-07-01')",
                },
                {
                    "partition_name": "task_instance_2025_07",
                    "bound_expr": "FOR VALUES FROM ('2025-07-01') TO ('2025-08-01')",
                },
                {
                    "partition_name": "task_instance_2026_07",
                    "bound_expr": "FOR VALUES FROM ('2026-07-01') TO ('2026-08-01')",
                },
            ]
        return []

    async def execute(self, sql, *args):
        self.executed.append(("execute", sql[:100]))
        if "DELETE FROM retry_task" in sql:
            return "DELETE 5"
        return "DROP TABLE"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass


class TestCleanupTaskPartitions:
    async def test_drops_old_partitions(self, monkeypatch):
        conn = _FakeConn()

        @contextlib.asynccontextmanager
        async def _session():
            yield conn

        monkeypatch.setattr("apihub_core.db.admin_db_session", _session)

        n = await repo_mod.cleanup_task_partitions(before=datetime(2026, 1, 1))
        assert n >= 1
        drops = [e for e in conn.executed if "DROP" in str(e)]
        assert len(drops) >= 1

    async def test_no_partitions(self, monkeypatch):
        class _EmptyConn(_FakeConn):
            async def fetch(self, sql, *args):
                return []

        conn = _EmptyConn()

        @contextlib.asynccontextmanager
        async def _session():
            yield conn

        monkeypatch.setattr("apihub_core.db.admin_db_session", _session)

        n = await repo_mod.cleanup_task_partitions(before=datetime(2026, 1, 1))
        assert n == 0


class TestCleanupRetryTasks:
    async def test_deletes_old_completed(self, monkeypatch):
        class _Conn(_FakeConn):
            def __init__(self):
                self.executed = []

            async def execute(self, sql, *args):
                self.executed.append(("execute", sql[:100]))
                return "DELETE 5"

        conn = _Conn()

        @contextlib.asynccontextmanager
        async def _session():
            yield conn

        monkeypatch.setattr("apihub_core.db.admin_db_session", _session)

        n = await repo_mod.cleanup_retry_tasks(before=datetime(2026, 1, 1))
        assert n == 5


class TestCleanupRoute:
    async def test_cleanup_requires_superadmin(self, client):
        resp = await client.post("/v1/admin/data/cleanup", json={})
        assert resp.status_code == 403

    async def test_cleanup_defaults(self, client, as_platform_admin, monkeypatch):
        captured = {}

        async def _parts(*, before):
            captured["parts_before"] = before
            return 2

        async def _retry(*, before):
            captured["retry_before"] = before
            return 5

        monkeypatch.setattr(repo_mod, "cleanup_task_partitions", _parts)
        monkeypatch.setattr(repo_mod, "cleanup_retry_tasks", _retry)

        resp = await client.post("/v1/admin/data/cleanup", json={})
        assert resp.status_code == 200
        body = resp.json()
        assert body["dropped_partitions"] == 2
        assert body["deleted_retry_tasks"] == 5
