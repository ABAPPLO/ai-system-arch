"""portal app/key 自助 —— 直写 app/api_key 表（RLS 按 caller tenant 隔离）。

不复用 auth /v1/apps 端点：那个走 X-API-Key middleware，而 Portal 是 JWT 人认证。
"""

import secrets

from apihub_core import db


async def create_app_for_user(*, tenant_id: str, name: str, app_type: str) -> dict:
    app_id = f"app_{secrets.token_hex(8)}"
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


async def list_apps_for_user(*, tenant_id: str) -> list[dict]:
    async with db.db_session() as conn:
        rows = await conn.fetch(
            "SELECT id, name, tenant_id, type, status FROM app "
            "WHERE tenant_id=$1 ORDER BY created_at DESC",
            tenant_id,
        )
    return [dict(r) for r in rows]


async def create_api_key_for_app(
    *, tenant_id: str, app_id: str, name: str
) -> dict:
    from auth.apikey import generate_api_key  # 复用 auth 的 key 生成纯函数

    plaintext, key_hash, display_prefix = generate_api_key()
    key_id = f"key_{secrets.token_hex(8)}"
    async with db.db_session() as conn:
        await conn.execute(
            """
            INSERT INTO api_key (id, tenant_id, app_id, key_prefix, key_hash, name, status)
            VALUES ($1, $2, $3, $4, $5, $6, 'active')
            """,
            key_id,
            tenant_id,
            app_id,
            display_prefix,
            key_hash,
            name,
        )
    return {
        "id": key_id,
        "app_id": app_id,
        "name": name,
        "key_prefix": display_prefix,
        "api_key": plaintext,
    }
