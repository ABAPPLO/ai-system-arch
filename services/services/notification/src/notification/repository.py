"""notification 数据访问 —— webhook_subscription 表 CRUD。"""

import secrets
from typing import Any

from apihub_core import db
from apihub_core.errors import ApiError, ErrorCode

from notification.models import WebhookResponse


async def list_webhooks(*, tenant_id: str) -> list[dict]:
    async with db.db_session() as conn:
        rows = await conn.fetch(
            "SELECT id, url, events, status, created_at FROM webhook_subscription"
            " WHERE tenant_id = $1 ORDER BY created_at DESC",
            tenant_id,
        )
    return [dict(r) for r in rows]


async def create_webhook(*, tenant_id: str, url: str, events: list[str], secret: str | None) -> dict:
    wh_id = f"wh_{secrets.token_hex(8)}"
    async with db.db_session() as conn:
        await conn.execute(
            "INSERT INTO webhook_subscription (id, tenant_id, url, events, secret)"
            " VALUES ($1, $2, $3, $4, $5)",
            wh_id, tenant_id, url, events, secret or "",
        )
    return {"id": wh_id, "url": url, "events": events, "status": "active"}


async def update_webhook(*, tenant_id: str, webhook_id: str, updates: dict) -> dict:
    sets = ", ".join(f"{k} = ${i+2}" for i, k in enumerate(updates))
    values = list(updates.values())
    async with db.db_session() as conn:
        row = await conn.fetchrow(
            f"UPDATE webhook_subscription SET {sets} WHERE id = $1 AND tenant_id = ${len(values)+2}"
            " RETURNING id, url, events, status, created_at",
            webhook_id, *values, tenant_id,
        )
    if not row:
        raise ApiError(ErrorCode.NOT_FOUND, "webhook not found")
    return dict(row)


async def delete_webhook(*, tenant_id: str, webhook_id: str) -> None:
    async with db.db_session() as conn:
        result = await conn.execute(
            "DELETE FROM webhook_subscription WHERE id = $1 AND tenant_id = $2",
            webhook_id, tenant_id,
        )
    if "DELETE 0" in result:
        raise ApiError(ErrorCode.NOT_FOUND, "webhook not found")
