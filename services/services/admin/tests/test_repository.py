"""repository 单测 —— 用 fake asyncpg pool 验证 SQL 拼接 + where 过滤。

不连真 PG：每个测试 monkeypatch db._pool 给一个 _FakeConn，断言被调用的 SQL/params。
"""

from datetime import datetime

import pytest
from admin import repository as repo
from admin.models import AuditQuery, AuditRecord


class _FakeConn:
    def __init__(self):
        self.queries = []  # 记录所有 (sql, params)
        self._return_row = None
        self._return_rows = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def transaction(self):
        return self

    async def start(self):
        pass

    async def commit(self):
        pass

    async def rollback(self):
        pass

    async def execute(self, sql, *args):
        self.queries.append((sql, args))
        return "INSERT 1"

    async def fetchrow(self, sql, *args):
        self.queries.append((sql, args))
        return self._return_row

    async def fetch(self, sql, *args):
        self.queries.append((sql, args))
        return self._return_rows


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return self._conn


@pytest.fixture
def fake_db(monkeypatch):
    conn = _FakeConn()
    pool = _FakePool(conn)
    from apihub_core import db as db_mod

    monkeypatch.setattr(db_mod, "_pool", pool)
    return conn


# ---------- 写 ----------


class TestRecord:
    async def test_record_inserts_audit_log(self, fake_db):
        entry = AuditRecord(
            tenant_id="t1",
            actor_type="user",
            actor_id="u1",
            action="create_tenant",
            resource_type="tenant",
            resource_id="t_new",
            detail={"name": "Acme"},
        )
        fake_db._return_row = {"id": 42}

        audit_id = await repo.record(entry)
        assert audit_id == 42

        # queries 包含 SET LOCAL + INSERT（admin_db_session 先 execute SET，再 fetchrow INSERT）
        insert_query = next(
            ((sql, p) for sql, p in fake_db.queries if "INSERT INTO audit_log" in sql),
            None,
        )
        assert insert_query is not None, "INSERT query should be recorded"
        sql, params = insert_query
        # 第 1 个参数是 tenant_id
        assert params[0] == "t1"
        # 第 7 个是 action
        assert params[6] == "create_tenant"
        # detail 序列化成 json string
        detail_idx = [i for i, p in enumerate(params) if isinstance(p, str) and '"name"' in p]
        assert detail_idx, "detail should be JSON-serialized"

    async def test_record_returns_zero_on_failure(self, fake_db, monkeypatch):
        """DB 抛 → 返回 0，不抛。"""

        async def _boom(*args, **kwargs):
            raise RuntimeError("pg down")

        fake_db.fetchrow = _boom
        entry = AuditRecord(
            tenant_id="t1",
            action="x",
            resource_type="y",
        )
        result = await repo.record(entry)
        assert result == 0


class TestRecordMany:
    async def test_empty_list(self, fake_db):
        n = await repo.record_many([])
        assert n == 0
        assert fake_db.queries == []

    async def test_batch_inserts(self, fake_db):
        entries = [
            AuditRecord(tenant_id="t1", action=f"a_{i}", resource_type="rt") for i in range(3)
        ]
        # execute 不抛 → 全部成功
        n = await repo.record_many(entries)
        assert n == 3
        # 1 个 SET（admin_db_session 启动）+ 3 个 INSERT
        inserts = [q for q in fake_db.queries if "INSERT" in q[0]]
        assert len(inserts) == 3

    async def test_partial_failure(self, fake_db, monkeypatch):
        """中间一条失败 → 计数只算成功的。"""
        call_count = [0]

        async def _flaky(sql, *args):
            call_count[0] += 1
            # 第 1 次 SET（admin_db_session 启动）→ OK
            # 第 2 条 INSERT（第 1 个 audit）→ OK
            # 第 3 条 INSERT（第 2 个 audit）→ raise
            # 第 4 条 INSERT（第 3 个 audit）→ OK
            if "INSERT" in sql and call_count[0] == 3:
                raise RuntimeError("transient")
            return "INSERT 1"

        fake_db.execute = _flaky
        entries = [
            AuditRecord(tenant_id="t1", action=f"a_{i}", resource_type="rt") for i in range(3)
        ]
        n = await repo.record_many(entries)
        assert n == 2  # 中间一条挂了


# ---------- 读 ----------


class TestBuildWhere:
    def test_empty_query(self):
        where, params = repo._build_where(AuditQuery(), viewer_tenant_id=None)
        assert where == ""
        assert params == []

    def test_viewer_tenant_forced(self):
        """普通用户：强制按 viewer_tenant_id 过滤，忽略 query.tenant_id。"""
        q = AuditQuery(tenant_id="t_other")  # 试图越权
        where, params = repo._build_where(q, viewer_tenant_id="t_self")
        assert "t_self" in params
        assert "t_other" not in params
        assert "tenant_id = $1" in where

    def test_admin_can_filter_by_any_tenant(self):
        q = AuditQuery(tenant_id="t_any")
        where, params = repo._build_where(q, viewer_tenant_id=None)
        assert "t_any" in params

    def test_all_filters(self):
        q = AuditQuery(
            tenant_id="t1",
            actor_id="u1",
            action="create",
            resource_type="tenant",
            resource_id="t_new",
            since=datetime(2026, 7, 1),
            until=datetime(2026, 7, 2),
        )
        where, params = repo._build_where(q, viewer_tenant_id=None)
        assert "tenant_id" in where
        assert "actor_id" in where
        assert "action" in where
        assert "resource_type" in where
        assert "resource_id" in where
        assert "created_at >=" in where
        assert "created_at <" in where
        assert len(params) == 7


class TestListEvents:
    async def test_returns_list_of_dicts(self, fake_db):
        fake_db._return_rows = [
            {
                "id": 1,
                "tenant_id": "t1",
                "actor_type": "user",
                "actor_id": "u1",
                "actor_name": "Alice",
                "action": "create_tenant",
                "resource_type": "tenant",
                "resource_id": "t1",
                "resource_name": None,
                "created_at": datetime(2026, 7, 1),
            },
        ]
        result = await repo.list_events(AuditQuery(), use_admin_session=True)
        assert len(result) == 1
        assert result[0]["action"] == "create_tenant"

    async def test_uses_db_session_for_normal_user(self, fake_db):
        """普通用户视角：调 db_session（带 RLS）。"""
        await repo.list_events(AuditQuery(), viewer_tenant_id="t_self", use_admin_session=False)
        # SQL 应含 tenant_id 过滤
        sql, params = fake_db.queries[0]
        assert "tenant_id = $1" in sql
        assert params[0] == "t_self"


class TestGetEvent:
    async def test_found(self, fake_db):
        fake_db._return_row = {
            "id": 1,
            "tenant_id": "t1",
            "actor_type": "user",
            "actor_id": "u1",
            "actor_name": "Alice",
            "actor_ip": "10.0.0.1",
            "auth_method": "api_key",
            "action": "create_tenant",
            "resource_type": "tenant",
            "resource_id": "t1",
            "resource_name": None,
            "env": None,
            "detail": {"k": "v"},
            "user_agent": "curl",
            "request_id": "r1",
            "trace_id": "t1",
            "created_at": datetime(2026, 7, 1),
        }
        result = await repo.get_event(1, use_admin_session=True)
        assert result["action"] == "create_tenant"
        # actor_ip 是普通 str（PG 返回 inet 类型时 repository 把它 str 化）
        assert result["actor_ip"] == "10.0.0.1"

    async def test_not_found_raises(self, fake_db):
        from apihub_core.errors import ApiError

        fake_db._return_row = None
        with pytest.raises(ApiError):
            await repo.get_event(999, use_admin_session=True)

    async def test_normal_user_tenant_filter(self, fake_db):
        fake_db._return_row = None  # 触发 404
        from apihub_core.errors import ApiError

        with pytest.raises(ApiError):
            await repo.get_event(1, viewer_tenant_id="t_self")

        # SQL 应含 tenant_id
        sql, params = fake_db.queries[0]
        assert "tenant_id" in sql
        assert "t_self" in params


class TestCount:
    async def test_count_returns_int(self, fake_db):
        fake_db._return_row = {"n": 42}
        n = await repo.count(AuditQuery(), use_admin_session=True)
        assert n == 42

    async def test_count_with_viewer(self, fake_db):
        fake_db._return_row = {"n": 0}
        await repo.count(AuditQuery(), viewer_tenant_id="t_self")
        sql, params = fake_db.queries[0]
        assert "t_self" in params


class TestStats:
    async def test_stats_aggregates(self, fake_db):
        """stats 跑 4 个查询：total(fetchrow) + top_actions/actors/by_day(fetch)。"""
        all_queries: list[tuple[str, tuple]] = []

        async def _fetchrow(sql, *args):
            all_queries.append((sql, args))
            return {"n": 100}

        async def _fetch(sql, *args):
            all_queries.append((sql, args))
            if "GROUP BY action" in sql:
                return [{"action": "create_tenant", "n": 30}]
            if "GROUP BY actor_id" in sql:
                return [{"actor_id": "u1", "actor_name": "Alice", "n": 5}]
            if "DATE(created_at)" in sql or "GROUP BY day" in sql:
                return [{"day": datetime(2026, 7, 1).date(), "n": 10}]
            return []

        fake_db.fetchrow = _fetchrow
        fake_db.fetch = _fetch

        result = await repo.stats(use_admin_session=True, days=7)
        assert result["total"] == 100
        assert isinstance(result["top_actions"], list)
        assert isinstance(result["top_actors"], list)
        assert isinstance(result["by_day"], list)
        # 1 个 COUNT(*) + 3 个 GROUP BY = 4 个查询
        assert len(all_queries) == 4
