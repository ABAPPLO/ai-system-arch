"""Kafka 消费者 —— 从 api-call-events topic 消费 → 推 Webhook。"""

import asyncio
import hmac
import hashlib
import json

import httpx
from apihub_core import db, kafka
from apihub_core.logging import get_logger

log = get_logger(__name__)

MAX_RETRIES = 3
BACKOFF_SECONDS = [5, 30, 120]


async def _deliver(url: str, payload: dict, secret: str) -> bool:
    """推送到 Webhook URL（带 HMAC 签名）。"""
    body = json.dumps(payload).encode()
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest() if secret else ""
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(url, content=body,
                             headers={"Content-Type": "application/json",
                                      "X-Webhook-Signature": sig})
            return r.status_code < 500
    except httpx.RequestError:
        return False


async def _get_active_webhooks() -> list[dict]:
    """取所有 active 的 webhook 订阅。"""
    async with db.admin_db_session() as conn:
        rows = await conn.fetch(
            "SELECT id, tenant_id, url, events, secret FROM webhook_subscription WHERE status = 'active'"
        )
    return [dict(r) for r in rows]


async def process_event(event: dict) -> None:
    """处理一条 api-call-event → 匹配 webhook → 推送。"""
    event_type = f"api.call.{'succeeded' if event.get('allowed', True) else 'failed'}"
    hooks = await _get_active_webhooks()
    matched = [h for h in hooks if event_type in h["events"] or "api.call.*" in h["events"]]
    if not matched:
        return

    payload = {"event": event_type, "data": event, "timestamp": asyncio.get_event_loop().time()}
    for hook in matched:
        for attempt in range(MAX_RETRIES):
            ok = await _deliver(hook["url"], payload, hook.get("secret") or "")
            if ok:
                log.info("webhook_delivered", webhook_id=hook["id"], event_type=event_type)
                break
            log.warning("webhook_retry", webhook_id=hook["id"], attempt=attempt + 1)
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(BACKOFF_SECONDS[attempt])


async def start_consumer(settings):
    """启动 Webhook Kafka 消费者（在 extra_lifespan 中注册）。"""
    loop = asyncio.get_event_loop()
    _client = None
    try:
        _client = kafka._get_client()
        async for msg in _client.consumer("api-call-events", group="notification-svc"):
            try:
                event = json.loads(msg.value)
                asyncio.ensure_future(process_event(event))
            except Exception:
                log.exception("webhook_process_error")
    except asyncio.CancelledError:
        pass
    finally:
        if _client:
            await _client.close()
