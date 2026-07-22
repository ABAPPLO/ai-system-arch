"""repository 单测 —— 验证状态机和参数校验（不连真 PG）。

repo 函数本身只是 SQL 包装，我们重点测：
  - change_status 的状态机合法性（在调 PG 前就抛）
  - add_member 的角色合法性
  - get_user_tenants / get_membership 等的纯函数行为

Integration 测试（TestTenantMetadataJsonbIntegration）需真 PG（make dev-up +
make db-apply），验证 tenant.metadata 经 jsonb codec 写入后 jsonb_typeof='object'
且 metadata->'quota'->>'day_limit' 可读（R2e Task 5 regression）。
"""

import asyncio
import os

import asyncpg
import pytest
from apihub_core.errors import ApiError
from tenant import repository as repo
from tenant.models import TenantCreate, TenantUpdate


class TestStatusMachinePrecheck:
    """change_status 在调 DB 之前先校验状态转换合法性。"""

    async def test_bad_status_rejected(self, monkeypatch):
        """非法 new_status → INVALID_PARAMS。"""
        with pytest.raises(ApiError) as exc:
            await repo.change_status("t1", "frozen")
        assert exc.value.code.name == "INVALID_PARAMS"

    async def test_closed_is_terminal(self, monkeypatch):
        """已 closed 的租户不能再变状态。"""

        async def _get_tenant(tid):
            return {
                "id": tid,
                "status": "closed",
                "name": "x",
                "slug": "x",
                "type": "internal",
                "tier": "standard",
                "metadata": {},
            }

        monkeypatch.setattr(repo, "get_tenant", _get_tenant)
        with pytest.raises(ApiError) as exc:
            await repo.change_status("t1", "active")
        assert exc.value.code.name == "CONFLICT"

    async def test_active_to_suspended_calls_db(self, monkeypatch):
        async def _get_tenant(tid):
            return {
                "id": tid,
                "status": "active",
                "name": "x",
                "slug": "x",
                "type": "internal",
                "tier": "standard",
                "metadata": {},
            }

        called = {}

        class _FakeCtx:
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

            async def execute(self, *args, **kwargs):
                return "SET"

            async def fetchrow(self, *args):
                called["fetchrow"] = args
                return {
                    "id": "t1",
                    "status": "suspended",
                    "name": "x",
                    "slug": "x",
                    "type": "internal",
                    "tier": "standard",
                    "metadata": {},
                    "parent_id": None,
                    "created_at": "2026-07-01",
                    "updated_at": "2026-07-01",
                }

        from apihub_core import db as db_mod

        class _FakePool:
            def __init__(self):
                self._conn = _FakeCtx()

            def acquire(self):
                return self._conn

        monkeypatch.setattr(db_mod, "_pool", _FakePool())
        monkeypatch.setattr(repo, "get_tenant", _get_tenant)

        result = await repo.change_status("t1", "suspended")
        assert result["status"] == "suspended"
        assert "fetchrow" in called


class TestRoleValidation:
    async def test_add_member_bad_role(self):
        with pytest.raises(ApiError):
            await repo.add_member("t1", "u1", "superuser")

    async def test_update_member_bad_role(self):
        with pytest.raises(ApiError):
            await repo.update_member_role("t1", "u1", "guest")


class TestTenantCreate:
    def test_normalized_type_bad_falls_back(self):
        payload = TenantCreate(id="t1", name="xx", slug="xx", type="garbage", tier="standard")
        assert payload.normalized_type() == "internal"

    def test_normalized_tier_bad_falls_back(self):
        payload = TenantCreate(id="t1", name="xx", slug="xx", type="internal", tier="platinum")
        assert payload.normalized_tier() == "standard"

    def test_id_pattern_rejects_spaces(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            TenantCreate(id="has space", name="x", slug="x")


class TestTenantUpdate:
    async def test_update_with_empty_payload_returns_current(self, monkeypatch):
        """空 payload 直接返回当前 tenant，不打 DB UPDATE。"""
        called = {"get_tenant": False}

        async def _get_tenant(tid):
            called["get_tenant"] = True
            return {
                "id": tid,
                "name": "n",
                "slug": "s",
                "type": "internal",
                "status": "active",
                "tier": "standard",
                "metadata": {},
            }

        monkeypatch.setattr(repo, "get_tenant", _get_tenant)

        result = await repo.update_tenant("t1", TenantUpdate())
        assert called["get_tenant"] is True
        assert result["id"] == "t1"

    async def test_update_bad_tier(self):
        with pytest.raises(ApiError):
            await repo.update_tenant("t1", TenantUpdate(tier="platinum"))


# ---------- Integration：tenant.metadata jsonb codec 端到端（R2e Task 5 regression）----------


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


class TestTenantMetadataJsonbIntegration:
    """R2e Task 5 regression: create_tenant/set_quota 写入的 metadata 在 PG 里
    须 jsonb_typeof='object' 且 metadata->'quota'->>'day_limit' 可读。

    回归背景：repository 早期版本定义 jsonb(data)=json.dumps(data, default=str)
    helper（已删），create_tenant/set_quota 经它传 str 给 $N::jsonb，生产 pool 的
    jsonb codec 会再次 encode → PG 存 jsonb string 而非 object →
    metadata->'quota'->>'day_limit' 返回 NULL。fake-pool 单测看不到此 bug。
    """

    @pytest.fixture(autouse=True)
    def _require_pg(self):
        """仅本 integration class 需真 PG；上方 fake-pool 单测不依赖 PG。"""
        if not asyncio.run(_pg_available()):
            pytest.skip("PG not available — run `make dev-up` first", allow_module_level=False)

    async def test_create_tenant_metadata_stored_as_jsonb_object(self, db_pool):
        payload = TenantCreate(
            id="t_r2e_t5_create",
            name="R2e T5 Create",
            slug="r2e-t5-create",
            type="internal",
            tier="standard",
            metadata={"quota": {"day_limit": 999}, "note": "r2e_t5"},
        )
        try:
            created = await repo.create_tenant(payload)
            assert created["id"] == "t_r2e_t5_create"

            async with db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT jsonb_typeof(metadata) AS kind, "
                    "metadata->'quota'->>'day_limit' AS day_limit, "
                    "metadata->>'note' AS note "
                    "FROM tenant WHERE id = $1",
                    "t_r2e_t5_create",
                )
            assert row is not None
            assert row["kind"] == "object", (
                f"metadata 应为 jsonb object，实际 jsonb_typeof={row['kind']!r}"
                "（说明被双重编码成 JSON 字符串了）"
            )
            assert (
                row["day_limit"] == "999"
            ), f"metadata->'quota'->>'day_limit' 应可读为 '999'，实际 {row['day_limit']!r}"
            assert row["note"] == "r2e_t5"
        finally:
            async with db_pool.acquire() as conn:
                await conn.execute("DELETE FROM tenant WHERE id = $1", "t_r2e_t5_create")

    async def test_set_quota_metadata_readable_as_jsonb_object(self, db_pool):
        # 先建一个 metadata 为空 dict 的租户
        payload = TenantCreate(
            id="t_r2e_t5_quota",
            name="R2e T5 Quota",
            slug="r2e-t5-quota",
            type="internal",
            tier="standard",
            metadata={},
        )
        try:
            await repo.create_tenant(payload)
            await repo.set_quota(
                "t_r2e_t5_quota",
                {"day_limit": 12345, "rate_limit": {"minute": 100}},
            )

            async with db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT jsonb_typeof(metadata) AS kind, "
                    "metadata->'quota'->>'day_limit' AS day_limit, "
                    "metadata->'quota'->'rate_limit'->>'minute' AS minute "
                    "FROM tenant WHERE id = $1",
                    "t_r2e_t5_quota",
                )
            assert row is not None
            assert (
                row["kind"] == "object"
            ), f"metadata 应为 jsonb object，实际 jsonb_typeof={row['kind']!r}"
            assert (
                row["day_limit"] == "12345"
            ), f"set_quota 后 day_limit 应可读为 '12345'，实际 {row['day_limit']!r}"
            assert row["minute"] == "100"
        finally:
            async with db_pool.acquire() as conn:
                await conn.execute("DELETE FROM tenant WHERE id = $1", "t_r2e_t5_quota")
