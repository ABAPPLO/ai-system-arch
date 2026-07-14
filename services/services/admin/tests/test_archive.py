"""审计归档测试 —— 路由 + repository 逻辑。"""

import contextlib
from datetime import datetime, timezone

from admin import repository as repo_mod


class _FakeConn:
    """模拟 asyncpg 连接 —— 支持 fetch + execute。"""

    def __init__(self, *, rows=None):
        self._rows = rows or []
        self.fetch_log = []
        self.execute_log = []

    async def fetch(self, sql, *args):
        self.fetch_log.append((sql[:100], args))
        return self._rows

    async def fetchrow(self, sql, *args):
        self.fetch_log.append((sql[:100], args))
        return self._rows[0] if self._rows else None

    async def execute(self, sql, *args):
        self.execute_log.append((sql[:100], args))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass


# ---------- archive endpoint ----------


class TestArchiveRoute:
    async def test_archive_superadmin_only(self, client):
        """普通用户调 archive → 403。"""
        resp = await client.post("/v1/admin/audit/archive", json={})
        assert resp.status_code == 403

    async def test_archive_default_cutoff(self, client, as_platform_admin, monkeypatch):
        """不传 before → 默认 180 天前。"""
        captured = {}

        async def _archive(cutoff):
            captured["cutoff"] = cutoff
            return 42

        monkeypatch.setattr(repo_mod, "archive_before", _archive)

        resp = await client.post("/v1/admin/audit/archive", json={})
        assert resp.status_code == 200
        body = resp.json()
        assert body["archived"] == 42
        assert "cutoff" in body

    async def test_archive_with_custom_before(self, client, as_platform_admin, monkeypatch):
        """传 before → 用指定日期。"""
        captured = {}

        async def _archive(cutoff):
            captured["cutoff"] = cutoff
            return 10

        monkeypatch.setattr(repo_mod, "archive_before", _archive)

        resp = await client.post(
            "/v1/admin/audit/archive",
            json={"before": "2026-01-15T00:00:00"},
        )
        assert resp.status_code == 200
        assert captured["cutoff"].isoformat().startswith("2026-01-15")

    async def test_archive_zero_result(self, client, as_platform_admin, monkeypatch):
        async def _archive(cutoff):
            return 0

        monkeypatch.setattr(repo_mod, "archive_before", _archive)

        resp = await client.post("/v1/admin/audit/archive", json={})
        assert resp.status_code == 200
        assert resp.json()["archived"] == 0

    async def test_archive_bad_date(self, client, as_platform_admin):
        resp = await client.post(
            "/v1/admin/audit/archive",
            json={"before": "not-a-date"},
        )
        assert resp.status_code == 422


# ---------- archive_before repository ----------


def _patch_db(monkeypatch, conn):
    """替换 db.admin_db_session 为返回给定 conn 的 async context manager。"""
    @contextlib.asynccontextmanager
    async def _session():
        yield conn

    monkeypatch.setattr("apihub_core.db.admin_db_session", _session)
    return conn


class TestArchiveBefore:
    async def test_no_records(self, monkeypatch):
        rows = [
            {"id": 1, "tenant_id": "t1", "actor_id": "u1",
             "action": "a", "resource_type": "r",
             "created_at": datetime(2025, 12, 1)},
        ]

        # 第一次 SELECT id 返回空 → 立即退出
        conn = _FakeConn(rows=[])
        _patch_db(monkeypatch, conn)

        n = await repo_mod.archive_before(datetime(2026, 1, 1))
        assert n == 0

    async def test_archives_one_batch(self, monkeypatch):
        rows = [
            {"id": i + 1, "tenant_id": "t1", "actor_id": "u1",
             "action": "create_api", "resource_type": "api",
             "created_at": datetime(2025, 12, 1)}
            for i in range(3)
        ]

        # 第一次 SELECT → 有 id → 第二次 SELECT * → 有完整数据
        return_seq = [
            [{"id": r["id"]} for r in rows],  # SELECT id
            rows,                              # SELECT *
        ]
        seq = [iter(return_seq).__next__, lambda *a: []]

        class _SeqConn(_FakeConn):
            def __init__(self):
                self._call = 0
                self.fetch_log = []
                self.execute_log = []

            async def fetch(self, sql, *args):
                self.fetch_log.append((sql[:80], args))
                if "DELETE" in sql:
                    return None
                idx = self._call
                self._call += 1
                return return_seq[idx] if idx < len(return_seq) else []

            async def execute(self, sql, *args):
                self.execute_log.append((sql[:80], args))

        conn = _SeqConn()
        _patch_db(monkeypatch, conn)

        uploaded = []

        async def _fake_put(bucket, key, data):
            uploaded.append((bucket, key))
            return True

        monkeypatch.setattr("apihub_core.oss.put_object", _fake_put)

        n = await repo_mod.archive_before(datetime(2026, 1, 1))
        assert n == 3
        assert len(uploaded) >= 1
        bucket, key = uploaded[0]
        assert bucket == "audit-archive"
        assert key.startswith("2025/12/")
        assert key.endswith(".jsonl.gz")
        assert len(conn.execute_log) == 1  # DELETE 被调用

    async def test_upload_failure_skips_delete(self, monkeypatch):
        rows = [
            {"id": 1, "tenant_id": "t1", "actor_id": "u1",
             "action": "create_api", "resource_type": "api",
             "created_at": datetime(2025, 12, 1)},
        ]

        return_seq = [
            [{"id": r["id"]} for r in rows],
            rows,
        ]

        class _SeqConn(_FakeConn):
            def __init__(self):
                self._call = 0
                self.fetch_log = []
                self.execute_log = []

            async def fetch(self, sql, *args):
                if "DELETE" in sql:
                    return None
                idx = self._call
                self._call += 1
                return return_seq[idx] if idx < len(return_seq) else []

            async def execute(self, sql, *args):
                self.execute_log.append((sql[:80], args))

        conn = _SeqConn()
        _patch_db(monkeypatch, conn)

        async def _fake_put(bucket, key, data):
            return False  # 上传失败

        monkeypatch.setattr("apihub_core.oss.put_object", _fake_put)

        n = await repo_mod.archive_before(datetime(2026, 1, 1))
        assert n == 0
        assert len(conn.execute_log) == 0  # DELETE 没有被调用

    async def test_multiple_tenants(self, monkeypatch):
        rows = [
            {"id": 1, "tenant_id": "t1", "actor_id": "u1",
             "action": "a", "resource_type": "r",
             "created_at": datetime(2025, 12, 1)},
            {"id": 2, "tenant_id": "t2", "actor_id": "u2",
             "action": "b", "resource_type": "r",
             "created_at": datetime(2025, 12, 1)},
        ]

        return_seq = [
            [{"id": r["id"]} for r in rows],
            rows,
        ]

        class _SeqConn(_FakeConn):
            def __init__(self):
                self._call = 0
                self.fetch_log = []
                self.execute_log = []

            async def fetch(self, sql, *args):
                if "DELETE" in sql:
                    return None
                idx = self._call
                self._call += 1
                return return_seq[idx] if idx < len(return_seq) else []

            async def execute(self, sql, *args):
                self.execute_log.append((sql[:80], args))

        conn = _SeqConn()
        _patch_db(monkeypatch, conn)

        uploaded = []

        async def _fake_put(bucket, key, data):
            uploaded.append(key)
            return True

        monkeypatch.setattr("apihub_core.oss.put_object", _fake_put)

        n = await repo_mod.archive_before(datetime(2026, 1, 1))
        assert n == 2
        assert len(uploaded) == 2
        assert any("t1" in k for k in uploaded)
        assert any("t2" in k for k in uploaded)

    async def test_multi_batch(self, monkeypatch):
        batch1 = [
            {"id": i + 1, "tenant_id": "t1", "actor_id": "u1",
             "action": "a", "resource_type": "r",
             "created_at": datetime(2025, 12, 1)}
            for i in range(1000)
        ]
        batch2 = [
            {"id": 1001, "tenant_id": "t1", "actor_id": "u1",
             "action": "a", "resource_type": "r",
             "created_at": datetime(2025, 12, 1)},
            {"id": 1002, "tenant_id": "t2", "actor_id": "u2",
             "action": "b", "resource_type": "r",
             "created_at": datetime(2025, 12, 1)},
        ]

        return_seq = [
            [{"id": r["id"]} for r in batch1],  # 第一批 SELECT id
            batch1,                              # 第一批 SELECT *
            [{"id": r["id"]} for r in batch2],  # 第二批 SELECT id
            batch2,                              # 第二批 SELECT *
        ]

        class _SeqConn(_FakeConn):
            def __init__(self):
                self._call = 0
                self.fetch_log = []
                self.execute_log = []

            async def fetch(self, sql, *args):
                if "DELETE" in sql:
                    return None
                idx = self._call
                self._call += 1
                return return_seq[idx] if idx < len(return_seq) else []

            async def execute(self, sql, *args):
                self.execute_log.append((sql[:80], args))

        conn = _SeqConn()
        _patch_db(monkeypatch, conn)

        upload_count = [0]

        async def _fake_put(bucket, key, data):
            upload_count[0] += 1
            return True

        monkeypatch.setattr("apihub_core.oss.put_object", _fake_put)

        n = await repo_mod.archive_before(datetime(2026, 1, 1))
        assert n == 1002
        assert upload_count[0] >= 2
