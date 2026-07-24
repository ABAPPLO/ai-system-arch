"""notification 数据访问 —— webhook_subscription 表 CRUD。"""

import secrets

from apihub_core import db
from apihub_core.errors import ApiError, ErrorCode

from notification import renderer as renderer_mod


async def list_webhooks(*, tenant_id: str) -> list[dict]:
    async with db.db_session() as conn:
        rows = await conn.fetch(
            "SELECT id, url, events, status, created_at FROM webhook_subscription"
            " WHERE tenant_id = $1 ORDER BY created_at DESC",
            tenant_id,
        )
    return [dict(r) for r in rows]


async def create_webhook(
    *, tenant_id: str, url: str, events: list[str], secret: str | None
) -> dict:
    """创建 webhook。

    secret=None → 平台生成（返明文一次，DB 存 AESGCM 加密）；
    client-supplied secret → 加密存同值（兼容老 client 传明文 secret 的场景）。
    """
    from apihub_core.crypto import encrypt_secret

    wh_id = f"wh_{secrets.token_hex(8)}"
    plaintext = secret if secret else secrets.token_urlsafe(32)
    encrypted = encrypt_secret(plaintext)
    async with db.db_session() as conn:
        await conn.execute(
            "INSERT INTO webhook_subscription (id, tenant_id, url, events, secret_encrypted)"
            " VALUES ($1, $2, $3, $4, $5)",
            wh_id,
            tenant_id,
            url,
            events,
            encrypted,
        )
    return {"id": wh_id, "url": url, "events": events, "status": "active", "hmac_secret": plaintext}


async def update_webhook(*, tenant_id: str, webhook_id: str, updates: dict) -> dict:
    sets = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(updates))
    values = list(updates.values())
    async with db.db_session() as conn:
        row = await conn.fetchrow(
            # column names come from a Pydantic-validated allowlist (model_dump), not user input
            f"UPDATE webhook_subscription SET {sets} WHERE id = $1 AND tenant_id = ${len(values) + 2}"  # noqa: S608
            " RETURNING id, url, events, status, created_at",
            webhook_id,
            *values,
            tenant_id,
        )
    if not row:
        raise ApiError(ErrorCode.NOT_FOUND, "webhook not found")
    return dict(row)


async def delete_webhook(*, tenant_id: str, webhook_id: str) -> None:
    async with db.db_session() as conn:
        result = await conn.execute(
            "DELETE FROM webhook_subscription WHERE id = $1 AND tenant_id = $2",
            webhook_id,
            tenant_id,
        )
    if "DELETE 0" in result:
        raise ApiError(ErrorCode.NOT_FOUND, "webhook not found")


async def list_channel_configs(*, tenant_id: str) -> list[dict]:
    async with db.db_session() as conn:
        rows = await conn.fetch(
            "SELECT id, channel_type, name, config, status, created_at"
            " FROM notification_channel_config WHERE tenant_id = $1 ORDER BY created_at DESC",
            tenant_id,
        )
    return [dict(r) for r in rows]


async def create_channel_config(
    *, tenant_id: str, channel_type: str, name: str, config: dict, status: str
) -> dict:
    cc_id = f"cc_{secrets.token_hex(8)}"
    async with db.db_session() as conn:
        await conn.execute(
            "INSERT INTO notification_channel_config (id, tenant_id, channel_type, name, config, status)"
            " VALUES ($1, $2, $3, $4, $5, $6)",
            cc_id,
            tenant_id,
            channel_type,
            name,
            config,
            status or "active",
        )
    return {
        "id": cc_id,
        "channel_type": channel_type,
        "name": name,
        "config": config,
        "status": status or "active",
    }


async def update_channel_config(*, tenant_id: str, config_id: str, updates: dict) -> dict:
    if not updates:
        raise ApiError(ErrorCode.INVALID_INPUT, "no fields to update", http_status=400)
    sets = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(updates))
    values = list(updates.values())
    async with db.db_session() as conn:
        row = await conn.fetchrow(
            # column names come from a Pydantic-validated allowlist (model_dump), not user input
            f"UPDATE notification_channel_config SET {sets} WHERE id = $1 AND tenant_id = ${len(values) + 2}"  # noqa: S608
            " RETURNING id, channel_type, name, config, status, created_at",
            config_id,
            *values,
            tenant_id,
        )
    if not row:
        raise ApiError(ErrorCode.NOT_FOUND, "channel config not found")
    return dict(row)


async def delete_channel_config(*, tenant_id: str, config_id: str) -> None:
    async with db.db_session() as conn:
        result = await conn.execute(
            "DELETE FROM notification_channel_config WHERE id = $1 AND tenant_id = $2",
            config_id,
            tenant_id,
        )
    if "DELETE 0" in result:
        raise ApiError(ErrorCode.NOT_FOUND, "channel config not found")


async def get_active_channel_config(*, tenant_id: str, channel_type: str) -> dict | None:
    """send 时解析 tenant 的 active 渠道配置；无则 None（email 由 caller/env 回退，dingtalk 无则 send 返失败）。"""
    async with db.db_session() as conn:
        row = await conn.fetchrow(
            "SELECT config FROM notification_channel_config"
            " WHERE tenant_id = $1 AND channel_type = $2 AND status = 'active'",
            tenant_id,
            channel_type,
        )
    return dict(row["config"]) if row else None


async def insert_notification_log(
    *,
    tenant_id: str,
    template_code: str,
    channel_type: str,
    recipient: str,
    status: str,
    error: str,
    provider_msg_id: str,
) -> None:
    log_id = f"nl_{secrets.token_hex(8)}"
    async with db.db_session() as conn:
        await conn.execute(
            "INSERT INTO notification_log (id, tenant_id, template_code, channel_type, recipient,"
            " status, error, provider_msg_id) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
            log_id,
            tenant_id,
            template_code,
            channel_type,
            recipient,
            status,
            error,
            provider_msg_id,
        )


async def render_template(
    *, code: str, channel_type: str, variables: dict, locale: str
) -> tuple[str, str]:
    """admin 读模板并渲染（routes 经此调用，便于测试 monkeypatch）。"""
    async with db.admin_db_session() as conn:
        return await renderer_mod.render(
            conn, code=code, channel_type=channel_type, variables=variables, locale=locale
        )
