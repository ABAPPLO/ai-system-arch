"""auth 身份业务单测（fake_redis + dev 栈 PG）。需 make dev-up。

identity.create_user / verify_email / login 通过 asyncpg 直连真 PG（admin_db_session，
绕 RLS 写 user_account / tenant_member），Redis 用 fakeredis。模块级 skip：无 PG 时整文件跳过。
"""

import asyncio
import os

import asyncpg
import pytest
from auth import identity

# dev 栈 PG 暴露在 host 15433（容器内 5432）。可经 TEST_PG_DSN 覆盖。
TEST_PG_DSN = os.environ.get(
    "TEST_PG_DSN",
    "postgresql://apihub:apihub_dev_pwd@localhost:15433/apihub",
)
# 测试用固定邮箱 —— 每个测试前后清理，保证可重跑。
_TEST_EMAILS = ("new@example.com", "dup@example.com", "v@example.com", "l@example.com")


async def _pg_available() -> bool:
    try:
        conn = await asyncpg.connect(TEST_PG_DSN)
        await conn.fetchval("SELECT 1")
        await conn.close()
        return True
    except Exception:
        return False


@pytest.fixture(scope="module", autouse=True)
def require_pg():
    """无 PG 则整模块 skip（不破坏 test_apikey 等不需 PG 的模块）。"""
    if not asyncio.run(_pg_available()):
        pytest.skip("PG not available — run `make dev-up` first", allow_module_level=True)


@pytest.fixture
async def db_pool(monkeypatch):
    """真 PG pool，注入 db._pool（admin_db_session 据此绕 RLS）。"""
    from apihub_core import db

    pool = await asyncpg.create_pool(TEST_PG_DSN, min_size=1, max_size=2)
    monkeypatch.setattr(db, "_pool", pool)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture(autouse=True)
async def cleanup_test_users(db_pool):
    """每测试前后删测试用户及其 tenant_member/user_consent，保证重复运行幂等。"""
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM tenant_member WHERE user_id IN "
            "(SELECT id FROM user_account WHERE email = ANY($1))",
            list(_TEST_EMAILS),
        )
        await conn.execute(
            "DELETE FROM user_consent WHERE user_id IN "
            "(SELECT id FROM user_account WHERE email = ANY($1))",
            list(_TEST_EMAILS),
        )
        await conn.execute(
            "DELETE FROM user_account WHERE email = ANY($1)",
            list(_TEST_EMAILS),
        )
    yield
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM tenant_member WHERE user_id IN "
            "(SELECT id FROM user_account WHERE email = ANY($1))",
            list(_TEST_EMAILS),
        )
        await conn.execute(
            "DELETE FROM user_consent WHERE user_id IN "
            "(SELECT id FROM user_account WHERE email = ANY($1))",
            list(_TEST_EMAILS),
        )
        await conn.execute(
            "DELETE FROM user_account WHERE email = ANY($1)",
            list(_TEST_EMAILS),
        )


@pytest.mark.asyncio
async def test_register_creates_pending_user(fake_redis):
    user = await identity.create_user(
        email="new@example.com", password="secret123", phone="13800000000", name="New"
    )
    assert user["status"] == "pending"
    assert user["verification_level"] == "email"
    assert await fake_redis.get(f"t:verify:{user['verify_token']}") == user["user_id"]


@pytest.mark.asyncio
async def test_register_duplicate_email_raises(fake_redis):
    await identity.create_user(
        email="dup@example.com", password="secret123", phone="138", name="A"
    )
    from apihub_core.errors import ApiError

    with pytest.raises(ApiError):
        await identity.create_user(
            email="dup@example.com", password="secret123", phone="139", name="B"
        )


@pytest.mark.asyncio
async def test_verify_email_activates_and_joins_external_public(fake_redis):
    user = await identity.create_user(
        email="v@example.com", password="secret123", phone="138", name="V"
    )
    result = await identity.verify_email(user["verify_token"])
    assert result["status"] == "active"
    assert result["tenant_id"] == "external-public"


@pytest.mark.asyncio
async def test_login_unverified_raises(fake_redis):
    await identity.create_user(
        email="l@example.com", password="secret123", phone="138", name="L"
    )
    from apihub_core.errors import ApiError

    with pytest.raises(ApiError):
        await identity.login(email="l@example.com", password="secret123")


@pytest.mark.asyncio
async def test_login_active_user_returns_jwt(fake_redis):
    """happy path：注册 → 验证 → 登录，拿到 JWT 且可解码（验证 Task1 jwt_utils 集成）。"""
    from apihub_core import jwt_utils
    from apihub_core.config import get_settings

    user = await identity.create_user(
        email="l@example.com", password="secret123", phone="138", name="L"
    )
    await identity.verify_email(user["verify_token"])
    result = await identity.login(email="l@example.com", password="secret123")

    assert "access_token" in result
    assert result["user"]["tenant_id"] == "external-public"
    decoded = jwt_utils.decode_token(result["access_token"], get_settings().jwt_secret)
    assert decoded["user_id"] == result["user"]["id"]
    assert decoded["tenant_id"] == "external-public"


@pytest.mark.asyncio
async def test_anonymize_user_hides_pii(fake_redis):
    from apihub_core import db as db_mod

    user = await identity.create_user(
        email="new@example.com", password="secret123", phone="13800000000", name="New"
    )
    uid = user["user_id"]
    await identity.anonymize_user(user_id=uid)

    async with db_mod.admin_db_session() as conn:
        row = await conn.fetchrow(
            "SELECT email, phone, name, password_hash, status FROM user_account WHERE id=$1", uid,
        )
    assert row["email"].endswith("@anonymized")
    assert row["phone"] == ""
    assert row["name"] == "Deleted User"
    assert row["password_hash"] == ""
    assert row["status"] == "deleted"


@pytest.mark.asyncio
async def test_anonymize_user_removes_tenant_membership(fake_redis):
    from apihub_core import db as db_mod

    user = await identity.create_user(
        email="new@example.com", password="secret123", phone="138", name="Del"
    )
    uid = user["user_id"]
    await identity.verify_email(user["verify_token"])

    async with db_mod.admin_db_session() as conn:
        before = await conn.fetchval(
            "SELECT COUNT(*) FROM tenant_member WHERE user_id = $1", uid,
        )
    assert before > 0

    await identity.anonymize_user(user_id=uid)

    async with db_mod.admin_db_session() as conn:
        after = await conn.fetchval(
            "SELECT COUNT(*) FROM tenant_member WHERE user_id = $1", uid,
        )
    assert after == 0


@pytest.mark.asyncio
async def test_anonymize_nonexistent_user_raises(fake_redis):
    from apihub_core.errors import ApiError

    with pytest.raises(ApiError) as exc:
        await identity.anonymize_user(user_id="u_nonexistent")
    assert exc.value.http_status == 404


@pytest.mark.asyncio
async def test_export_user_data_returns_account(fake_redis):
    """导出包含用户账户信息。"""
    user = await identity.create_user(
        email="new@example.com", password="secret123", phone="13800000000", name="Export",
    )
    uid = user["user_id"]
    await identity.verify_email(user["verify_token"])

    data = await identity.export_user_data(user_id=uid)

    assert data["user_id"] == uid
    assert data["account"]["email"].startswith("new@")
    assert data["account"]["name"] == "Export"
    assert data["account"]["phone"] == "13800000000"
    assert data["account"]["status"] == "active"
    assert "exported_at" in data


@pytest.mark.asyncio
async def test_export_user_data_includes_tenants(fake_redis):
    """导出包含租户关系。"""
    user = await identity.create_user(
        email="new@example.com", password="secret123", phone="138", name="Tenant",
    )
    uid = user["user_id"]
    await identity.verify_email(user["verify_token"])

    data = await identity.export_user_data(user_id=uid)

    assert len(data["tenants"]) >= 1
    assert any(t["tenant_id"] == "external-public" for t in data["tenants"])


@pytest.mark.asyncio
async def test_export_nonexistent_user_raises(fake_redis):
    from apihub_core.errors import ApiError

    with pytest.raises(ApiError) as exc:
        await identity.export_user_data(user_id="u_nonexistent")
    assert exc.value.http_status == 404


@pytest.mark.asyncio
async def test_anonymize_user_scrubs_notification_log(fake_redis):
    """anonymize 清该用户邮箱作为 recipient 的 notification_log，干扰行（他人 email）留。"""
    from apihub_core import db as db_mod

    user = await identity.create_user(
        email="scrub@example.com", password="secret123", phone="139", name="Scrub"
    )
    uid = user["user_id"]

    try:
        async with db_mod.admin_db_session() as conn:
            await conn.execute(
                "INSERT INTO notification_log (id, tenant_id, template_code, channel_type,"
                " recipient, status) VALUES ($1,$2,$3,$4,$5,$6), ($7,$8,$9,$10,$11,$12)",
                "nl_a", "external-public", "task_complete", "email", "scrub@example.com", "sent",
                "nl_b", "external-public", "task_complete", "email", "other@example.com", "sent",
            )
            before = await conn.fetchval(
                "SELECT COUNT(*) FROM notification_log WHERE recipient=$1", "scrub@example.com",
            )
        assert before == 1

        await identity.anonymize_user(user_id=uid)

        async with db_mod.admin_db_session() as conn:
            after_target = await conn.fetchval(
                "SELECT COUNT(*) FROM notification_log WHERE recipient=$1", "scrub@example.com",
            )
            after_other = await conn.fetchval(
                "SELECT COUNT(*) FROM notification_log WHERE recipient=$1", "other@example.com",
            )
        assert after_target == 0
        assert after_other == 1
    finally:
        async with db_mod.admin_db_session() as conn:
            await conn.execute(
                "DELETE FROM notification_log WHERE id IN ('nl_a','nl_b')",
            )

