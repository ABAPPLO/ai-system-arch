"""repository 单测 —— 用 fake asyncpg pool 验证 SQL 拼接 + where 过滤。

不连真 PG：每个测试 monkeypatch db._pool 给一个 _FakeConn，断言被调用的 SQL/params。

Integration 测试（TestRecordJsonbIntegration）需真 PG（make dev-up + make db-apply），
验证 audit_log.detail 经 jsonb codec 写入后 jsonb_typeof='object'（R2e Task 5 regression）。
"""

import asyncio
import os
from datetime import datetime

import asyncpg
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
        # detail 直传 dict（让 asyncpg jsonb codec 序列化）—— 切勿预先 json.dumps，
        # 否则生产 codec 会二次编码 → jsonb typeof=string → detail->>'...' = NULL。
        assert params[11] == {"name": "Acme"}, (
            "detail should be passed as dict (codec handles serialization), "
            "not pre-serialized via json.dumps"
        )

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


# ---------- Integration：jsonb codec 端到端（R2e Task 5 regression）----------


# dev 栈 PG 暴露在 host 15433（容器内 5432）。可经 TEST_PG_DSN 覆盖。
PG_DSN = os.environ.get(
    "TEST_PG_DSN",
    "postgresql://apihub:apihub_dev_pwd@localhost:15433/apihub",
)


async def _pg_available() -> bool:
    try:
        conn = await asyncpg.connect(PG_DSN)
        try:
            await conn.fetchval("SELECT 1")
        finally:
            await conn.close()
        return True
    except OSError:
        return False
    except asyncpg.PostgresError:
        return False


@pytest.fixture
async def db_pool(monkeypatch):
    """真 PG pool（带 jsonb codec），注入 db._pool。

    必须带 _init_jsonb_codec（与 init_pool 一致）——否则 jsonb 走 asyncpg 默认
    text 编解码，掩盖生产-only 的 codec 双重编码 bug。参照 test_identity.py 的 db_pool。
    """
    from apihub_core import db

    pool = await asyncpg.create_pool(
        PG_DSN,
        min_size=1,
        max_size=2,
        init=db._init_jsonb_codec,
    )
    monkeypatch.setattr(db, "_pool", pool)
    try:
        yield pool
    finally:
        await pool.close()


class TestRecordJsonbIntegration:
    """R2e Task 5 regression: record() 写入的 detail 在 PG 里须 jsonb_typeof='object'。

    回归背景：record() 早期版本传 json.dumps(entry.detail, default=str)（str）给
    $12::jsonb，生产 pool 的 jsonb codec 会再次 encode 该字符串 → PG 存 jsonb string
    而非 object → detail->>'<key>' 返回 NULL。fake-pool 单测看不到此 bug。
    """

    @pytest.fixture(autouse=True)
    def _require_pg(self):
        """仅本 integration class 需真 PG；上方 fake-pool 单测不依赖 PG。"""
        if not asyncio.run(_pg_available()):
            pytest.skip("PG not available — run `make dev-up` first", allow_module_level=False)

    async def test_record_detail_stored_as_jsonb_object(self, db_pool):
        entry = AuditRecord(
            tenant_id="t_r2e_t5",
            actor_type="system",
            action="r2e_t5_jsonb_check",
            resource_type="test",
            resource_id="r2e_t5",
            detail={"reason": "r2e_t5", "count": 42, "nested": {"k": "v"}},
        )
        audit_id = await repo.record(entry)
        assert audit_id > 0, "record() 应返回新插入的 audit_log.id"
        try:
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT jsonb_typeof(detail) AS kind, "
                    "detail->>'reason' AS reason, "
                    "detail->>'count' AS cnt, "
                    "detail->'nested'->>'k' AS nested_k "
                    "FROM audit_log WHERE id = $1",
                    audit_id,
                )
            assert row is not None, f"audit row {audit_id} not found"
            assert row["kind"] == "object", (
                f"detail 应为 jsonb object，实际 jsonb_typeof={row['kind']!r}"
                "（说明被双重编码成 JSON 字符串了）"
            )
            assert row["reason"] == "r2e_t5"
            assert row["cnt"] == "42"
            assert row["nested_k"] == "v"
        finally:
            async with db_pool.acquire() as conn:
                await conn.execute("DELETE FROM audit_log WHERE id = $1", audit_id)

    async def test_record_many_details_stored_as_jsonb_object(self, db_pool):
        entries = [
            AuditRecord(
                tenant_id="t_r2e_t5",
                actor_type="system",
                action=f"r2e_t5_many_{i}",
                resource_type="test",
                resource_id=f"r2e_t5_many_{i}",
                detail={"idx": i, "label": f"row-{i}"},
            )
            for i in range(3)
        ]
        n = await repo.record_many(entries)
        assert n == 3
        try:
            async with db_pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT jsonb_typeof(detail) AS kind, "
                    "detail->>'idx' AS idx, "
                    "detail->>'label' AS label "
                    "FROM audit_log "
                    "WHERE action LIKE 'r2e_t5_many_%' AND tenant_id = 't_r2e_t5'"
                )
            assert len(rows) == 3
            for r in rows:
                assert (
                    r["kind"] == "object"
                ), f"record_many detail 应为 jsonb object，实际 {r['kind']!r}"
                assert r["idx"] is not None and r["label"] is not None
        finally:
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM audit_log WHERE action LIKE 'r2e_t5_many_%' "
                    "AND tenant_id = 't_r2e_t5'"
                )
