"""数据库访问层 —— 把 SQL 集中在这里。

verify_* 函数用 admin_db_session（跨租户），CRUD 用 db_session（同租户）。
"""

import contextlib
from datetime import UTC, datetime

from apihub_core import db
from apihub_core.errors import ApiError, ErrorCode

from auth.apikey import hash_api_key


async def verify_api_key_record(api_key_plaintext: str) -> dict | None:
    """跨租户查 APIKey —— 用于 dispatcher 调用 auth 的 /v1/apikey/verify。

    Returns None if not found / disabled / expired.
    """
    key_hash = hash_api_key(api_key_plaintext)

    async with db.admin_db_session() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                ak.id, ak.app_id, ak.scopes, ak.status, ak.expires_at,
                ak.tenant_id,
                t.type  AS tenant_type,
                t.metadata->>'is_platform_admin' AS platform_admin_flag
            FROM api_key ak
            JOIN tenant t ON t.id = ak.tenant_id
            WHERE ak.key_hash = $1
            """,
            key_hash,
        )

        if not row:
            return None

        if row["status"] != "active":
            return None

        if row["expires_at"] and row["expires_at"] < datetime.now(UTC):
            return None

        # 异步更新 last_used_at（best-effort，不影响鉴权）
        with contextlib.suppress(Exception):
            await conn.execute(
                "UPDATE api_key SET last_used_at = NOW() WHERE id = $1",
                row["id"],
            )

        return {
            "is_active": True,
            "tenant_id": row["tenant_id"],
            "tenant_type": row["tenant_type"],
            "app_id": row["app_id"],
            "is_platform_admin": row["platform_admin_flag"] == "true",
            "scopes": list(row["scopes"] or []),
            "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
        }


async def get_tenant_home_region(tenant_id: int) -> str | None:
    """跨租户查 tenant 的 home_region（admin session，不触发 RLS）。

    R0a §2.4：opt-in 审计 —— 每次 /internal/auth/check 跨租户读都留一条 audit_log。
    """
    async with db.admin_db_session(audit_reason="cross-tenant api-key verify") as conn:
        row = await conn.fetchval(
            "SELECT home_region FROM tenant WHERE id = $1",
            tenant_id,
        )
        return row


async def create_api_key(
    *,
    key_id: str,
    app_id: str,
    tenant_id: str,
    name: str,
    key_hash: str,
    display_prefix: str,
    scopes: list[str],
    expires_at: datetime | None,
) -> dict:
    """插入新 APIKey（同租户 RLS 校验：调用方必须属于 app_id 的租户）。"""
    async with db.db_session() as conn:
        # 先校验 app 属于本租户（RLS 自动过滤）
        app = await conn.fetchrow("SELECT id, tenant_id FROM app WHERE id = $1", app_id)
        if not app:
            raise ApiError(
                ErrorCode.NOT_FOUND,
                f"app {app_id} not found in your tenant",
            )

        await conn.execute(
            """
            INSERT INTO api_key (
                id, tenant_id, app_id, key_prefix, key_hash,
                name, scopes, status, expires_at, created_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, 'active', $8, NOW())
            """,
            key_id,
            tenant_id,
            app_id,
            display_prefix,
            key_hash,
            name,
            scopes,
            expires_at,
        )

        return {
            "id": key_id,
            "app_id": app_id,
            "name": name,
            "scopes": scopes,
            "display_prefix": display_prefix,
            "expires_at": expires_at,
            "created_at": datetime.now(UTC).isoformat(),
        }


async def list_api_keys_for_app(app_id: str) -> list[dict]:
    """列出 app 的所有 APIKey（RLS 过滤本租户）。"""
    async with db.db_session() as conn:
        rows = await conn.fetch(
            """
            SELECT id, app_id, name, scopes, key_prefix AS display_prefix,
                   status, last_used_at, expires_at, created_at, revoked_at
            FROM api_key
            WHERE app_id = $1
            ORDER BY created_at DESC
            """,
            app_id,
        )
    return [dict(r) for r in rows]


async def revoke_api_key(key_id: str) -> dict:
    """吊销 APIKey（同租户）。返回 revoked_key 信息用于清缓存。"""
    async with db.db_session() as conn:
        row = await conn.fetchrow(
            """
            UPDATE api_key
            SET status = 'revoked', revoked_at = NOW()
            WHERE id = $1 AND status = 'active'
            RETURNING id, app_id, key_hash
            """,
            key_id,
        )
    if not row:
        raise ApiError(
            ErrorCode.NOT_FOUND,
            f"active api_key {key_id} not found",
        )
    return dict(row)


async def create_app(
    *, app_id: str, tenant_id: str, name: str, app_type: str
) -> dict:
    """插入新 app（同租户 RLS 由 db_session 的 SET LOCAL app.tenant_id 保证）。"""
    async with db.db_session() as conn:
        await conn.execute(
            """
            INSERT INTO app (id, tenant_id, name, type, status)
            VALUES ($1, $2, $3, $4, 'active')
            """,
            app_id,
            tenant_id,
            name,
            app_type,
        )
    return {
        "id": app_id,
        "name": name,
        "tenant_id": tenant_id,
        "type": app_type,
        "status": "active",
    }


async def list_apps_for_tenant(tenant_id: str) -> list[dict]:
    """列出本租户所有 app（RLS 过滤）。"""
    async with db.db_session() as conn:
        rows = await conn.fetch(
            "SELECT id, name, tenant_id, type, status FROM app "
            "WHERE tenant_id = $1 ORDER BY created_at DESC",
            tenant_id,
        )
    return [dict(r) for r in rows]
