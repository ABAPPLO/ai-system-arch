"""PG RLS 集成测试 —— 验证租户隔离真生效。

需要：
    1. PG 已起（make dev-up）
    2. schema 已初始化（scripts/init-db/01-schema.sql）
    3. 种子数据已加载（scripts/init-db/02-seed.sql）

跑：
    cd services/libs/apihub-core
    pip install -e .[test]
    pytest tests/test_db_rls.py -v

或只跑集成测试：
    pytest -m integration -v

跳过集成测试：
    pytest -m "not integration"
"""

import asyncio
import os
from contextlib import asynccontextmanager

import asyncpg
import pytest

pytestmark = pytest.mark.integration

PG_DSN = os.environ.get(
    "TEST_PG_DSN",
    "postgresql://apihub:apihub_dev_pwd@localhost:5432/apihub",
)


@asynccontextmanager
async def _connect():
    conn = await asyncpg.connect(PG_DSN)
    try:
        yield conn
    finally:
        await conn.close()


async def _pg_available() -> bool:
    try:
        async with _connect() as conn:
            await conn.fetchval("SELECT 1")
        return True
    except Exception:
        return False


# 设备：跳过条件（模块级，无 PG 时整个文件 skip）
@pytest.fixture(scope="module", autouse=True)
def require_pg():
    if not asyncio.run(_pg_available()):
        pytest.skip("PG not available — run `make dev-up` first", allow_module_level=True)


class TestRLSIsolation:
    """关键集成测试：证明 RLS 真隔离租户数据。"""

    async def test_tenant_a_cannot_see_tenant_b(self):
        async with _connect() as conn, conn.transaction():
            await conn.execute("SET LOCAL app.tenant_id = 'tenant_a'")
            rows = await conn.fetch("SELECT id, tenant_id FROM api ORDER BY id")

        tenant_ids = {r["tenant_id"] for r in rows}
        assert tenant_ids == {"tenant_a"}, f"泄漏了其他租户: {tenant_ids - {'tenant_a'}}"

    async def test_tenant_b_cannot_see_tenant_a(self):
        async with _connect() as conn, conn.transaction():
            await conn.execute("SET LOCAL app.tenant_id = 'tenant_b'")
            rows = await conn.fetch("SELECT id, tenant_id FROM api")

        tenant_ids = {r["tenant_id"] for r in rows}
        assert tenant_ids == {"tenant_b"}

    async def test_external_tenant_isolation(self):
        """外部租户看不到任何 internal 接口。"""
        async with _connect() as conn, conn.transaction():
            await conn.execute("SET LOCAL app.tenant_id = 'tenant_ext_1'")
            rows = await conn.fetch("SELECT id FROM api")

        assert rows == [], "外部租户不应看到任何接口（除非显式授权）"

    async def test_no_tenant_context_sees_nothing(self):
        """没 set tenant_id 时应看不到任何数据（不是看全部！）。"""
        async with _connect() as conn, conn.transaction():
            # 不 set，current_setting 默认空字符串
            rows = await conn.fetch("SELECT id FROM api")

        # RLS WHERE tenant_id = '' —— 看不到任何行
        assert rows == []


class TestPlatformAdminBypass:
    async def test_admin_sees_all_tenants(self):
        async with _connect() as conn, conn.transaction():
            await conn.execute("SET LOCAL app.tenant_id = ''")
            await conn.execute("SET LOCAL app.is_platform_admin = 'true'")
            rows = await conn.fetch("SELECT tenant_id, count(*) FROM api GROUP BY tenant_id")

        tenants = {r["tenant_id"] for r in rows}
        assert {"tenant_a", "tenant_b"}.issubset(tenants)

    async def test_admin_can_insert_any_tenant(self):
        """超管可跨租户写入（运维场景）。"""
        async with _connect() as conn, conn.transaction():
            await conn.execute("SET LOCAL app.tenant_id = ''")
            await conn.execute("SET LOCAL app.is_platform_admin = 'true'")
            await conn.execute(
                """
                INSERT INTO api (id, tenant_id, name, category, base_path, status, visibility)
                VALUES ('api_test_admin', 'tenant_a', 'admin test', 'temp', '/temp-' || md5(random()::text), 'draft', 'private')
                ON CONFLICT (id) DO NOTHING
                """
            )
            # 清理
            await conn.execute("DELETE FROM api WHERE id = 'api_test_admin'")


class TestRLSEnforcement:
    """验证：即使业务 SQL 不写 WHERE tenant_id，RLS 也强制过滤。"""

    async def test_unqualified_select_filters_automatically(self):
        """业务代码忘写 WHERE 时，RLS 兜底。"""
        async with _connect() as conn, conn.transaction():
            await conn.execute("SET LOCAL app.tenant_id = 'tenant_a'")
            # 没写 WHERE —— 但 RLS 仍然只返回 tenant_a 的
            rows = await conn.fetch("SELECT * FROM api")

        assert len(rows) > 0
        assert all(r["tenant_id"] == "tenant_a" for r in rows)

    async def test_cross_tenant_insert_rejected(self):
        """tenant_a 的会话不能插入 tenant_b 的数据（WITH CHECK 阻断）。"""
        async with _connect() as conn, conn.transaction():
            await conn.execute("SET LOCAL app.tenant_id = 'tenant_a'")
            with pytest.raises(Exception) as exc:
                await conn.execute(
                    """
                        INSERT INTO api (id, tenant_id, name, category, base_path, status)
                        VALUES ('api_cross', 'tenant_b', 'cross', 'temp', '/cross', 'draft')
                        """
                )
            # PG 行级安全策略违反
            assert "rls" in str(exc.value).lower() or "policy" in str(exc.value).lower()
            # transaction rollback，不需要 cleanup


class TestRLSViaDbSession:
    """通过 apihub_core.db.db_session() 的端到端测试（更接近业务真实用法）。"""

    async def test_db_session_auto_sets_tenant(self, monkeypatch, tenant_a):
        """db_session() 内部自动 SET LOCAL app.tenant_id，业务无需手动写。"""
        from apihub_core import db
        from apihub_core.tenant import set_tenant_context

        # 用真实 pool 连到 dev PG
        pool = await asyncpg.create_pool(
            PG_DSN, min_size=1, max_size=2, init=db._init_jsonb_codec,
        )
        monkeypatch.setattr(db, "_pool", pool)

        try:
            set_tenant_context(tenant_a)
            async with db.db_session() as conn:
                rows = await conn.fetch("SELECT id, tenant_id FROM api")

            # 业务代码完全没写 WHERE tenant_id，依然只看到 tenant_a
            assert all(r["tenant_id"] == "tenant_a" for r in rows)
            assert len(rows) > 0
        finally:
            await pool.close()

    async def test_meta_db_session_bypasses_rls_without_tenant_ctx(self, monkeypatch, tenant_a):
        """meta_db_session 绕过 RLS，无租户上下文也能跨租户可见所有 published api 元数据。

        场景：dispatcher resolver 跨租户路由解析 —— external-public caller 也要能
        resolve 到 tenant_a 的 public API。授权由应用层（dispatcher visibility）做。
        区别于 admin_db_session：meta 不写审计，面向平台网关读路径。
        """
        from apihub_core import db
        from apihub_core.tenant import clear_tenant_context, set_tenant_context

        pool = await asyncpg.create_pool(
            PG_DSN, min_size=1, max_size=2, init=db._init_jsonb_codec,
        )
        monkeypatch.setattr(db, "_pool", pool)

        try:
            # 先用 tenant_a 上下文确认 RLS 正常只返回 tenant_a（对照组）
            set_tenant_context(tenant_a)
            async with db.db_session() as conn:
                rows = await conn.fetch("SELECT tenant_id FROM api")
            assert all(r["tenant_id"] == "tenant_a" for r in rows)

            # 清空上下文，meta_db_session 应仍能看到所有租户（绕 RLS）
            clear_tenant_context()
            async with db.meta_db_session() as conn:
                rows = await conn.fetch("SELECT tenant_id FROM api GROUP BY tenant_id")

            tenants = {r["tenant_id"] for r in rows}
            # dev 种子至少有 tenant_a / tenant_b（见 02-seed.sql）
            assert {"tenant_a", "tenant_b"}.issubset(tenants), (
                f"meta_db_session 应跨租户可见，实际看到: {tenants}"
            )
        finally:
            await pool.close()


class TestRLSInjectionHardened:
    """R0a §2.5: db_session 用 set_config($1) 参数化，含引号的 tenant_id 不能注入/报错。"""

    async def test_db_session_handles_quote_in_tenant_id(self, monkeypatch):
        from apihub_core import db
        from apihub_core.tenant import TenantContext, set_tenant_context

        pool = await asyncpg.create_pool(
            PG_DSN, min_size=1, max_size=2, init=db._init_jsonb_codec,
        )
        monkeypatch.setattr(db, "_pool", pool)
        # 尝试 SQL 注入：旧 f-string 实现会拼进 SQL 破坏语句或改写 RLS
        evil = TenantContext(
            tenant_id="x', 'true'); -- ",
            tenant_type="internal",
            app_id="app_trading",
        )
        try:
            set_tenant_context(evil)
            async with db.db_session() as conn:
                rows = await conn.fetch("SELECT id, tenant_id FROM api")
            # 参数化后 evil 被当字面量：RLS 过滤到该(不存在)tenant → 空，无注入、无报错
            assert rows == [], f"注入面：意外返回行 {rows}"
        finally:
            await pool.close()

    async def test_db_session_still_filters_correctly_after_param_change(self, monkeypatch, tenant_a):
        from apihub_core import db
        from apihub_core.tenant import set_tenant_context

        pool = await asyncpg.create_pool(
            PG_DSN, min_size=1, max_size=2, init=db._init_jsonb_codec,
        )
        monkeypatch.setattr(db, "_pool", pool)
        try:
            set_tenant_context(tenant_a)
            async with db.db_session() as conn:
                rows = await conn.fetch("SELECT tenant_id FROM api")
            assert rows and all(r["tenant_id"] == "tenant_a" for r in rows)
        finally:
            await pool.close()


class TestAdminDbSessionAudit:
    """R0a §2.4: admin_db_session 可审计（opt-in），且不递归。"""

    async def test_no_audit_by_default(self, monkeypatch):
        from apihub_core import db

        pool = await asyncpg.create_pool(
            PG_DSN, min_size=1, max_size=2, init=db._init_jsonb_codec,
        )
        monkeypatch.setattr(db, "_pool", pool)
        try:
            async with _connect() as c, c.transaction():
                await c.execute(
                    "SELECT set_config('app.is_platform_admin', $1, true)", "true"
                )
                before = await c.fetchval(
                    "SELECT count(*) FROM audit_log WHERE action = 'admin_db_session'"
                )
            async with db.admin_db_session() as conn:
                await conn.fetchval("SELECT 1")
            async with _connect() as c, c.transaction():
                await c.execute(
                    "SELECT set_config('app.is_platform_admin', $1, true)", "true"
                )
                after = await c.fetchval(
                    "SELECT count(*) FROM audit_log WHERE action = 'admin_db_session'"
                )
            assert after == before, "默认不应写审计"
        finally:
            await pool.close()

    async def test_audits_when_reason_given(self, monkeypatch, tenant_a):
        from apihub_core import db
        from apihub_core.tenant import set_tenant_context

        pool = await asyncpg.create_pool(
            PG_DSN, min_size=1, max_size=2, init=db._init_jsonb_codec,
        )
        monkeypatch.setattr(db, "_pool", pool)
        set_tenant_context(tenant_a)
        try:
            async with _connect() as c, c.transaction():
                await c.execute(
                    "SELECT set_config('app.is_platform_admin', $1, true)", "true"
                )
                before = await c.fetchval(
                    "SELECT count(*) FROM audit_log WHERE action = 'admin_db_session'"
                )
            async with db.admin_db_session(audit_reason="cross-tenant key verify") as conn:
                await conn.fetchval("SELECT 1")
            async with _connect() as c, c.transaction():
                await c.execute(
                    "SELECT set_config('app.is_platform_admin', $1, true)", "true"
                )
                after = await c.fetchval(
                    "SELECT count(*) FROM audit_log WHERE action = 'admin_db_session'"
                )
            assert after == before + 1, "传 audit_reason 应写一条审计"
        finally:
            await pool.close()

    async def test_audit_failure_does_not_break_operation(self, monkeypatch, tenant_a):
        """审计写失败（如 audit_log 表不存在）不能影响业务操作。"""
        from apihub_core import db
        from apihub_core.tenant import set_tenant_context

        pool = await asyncpg.create_pool(
            PG_DSN, min_size=1, max_size=2, init=db._init_jsonb_codec,
        )
        monkeypatch.setattr(db, "_pool", pool)
        set_tenant_context(tenant_a)
        # 让 _write_admin_audit 内部 INSERT 报错：指向不存在的表
        monkeypatch.setattr(db, "_AUDIT_TABLE", "audit_log_does_not_exist")
        try:
            async with db.admin_db_session(audit_reason="x") as conn:
                val = await conn.fetchval("SELECT 1")
            assert val == 1  # 业务操作照常完成
        finally:
            await pool.close()
