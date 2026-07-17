"""Kafka 消费者 —— 从 api-call-events topic 消费 → 推 Webhook。"""

import asyncio
import hashlib
import hmac
import json
from contextlib import asynccontextmanager, suppress

import aiokafka
import httpx
from apihub_core import db
from apihub_core.config import get_settings
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


@asynccontextmanager
async def start_consumer(app):
    """作 extra_lifespan 传给 create_app：起后台消费 task，app 退出时取消。

    middleware 用 `async with extra_lifespan(app)`，故须是异步上下文管理器
    （@asynccontextmanager），不能是裸协程。消费循环放后台 task，避免阻塞 startup。
    """
    task = asyncio.create_task(_consume_loop())
    log.info("notification_consumer_started", topic="api-call-events", group="notification-svc")
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task


async def _consume_loop() -> None:
    """后台消费 api-call-events → 匹配 webhook → 推送（aiokafka 直连）。"""
    settings = get_settings()
    consumer = aiokafka.AIOKafkaConsumer(
        "api-call-events",
        bootstrap_servers=settings.kafka_brokers,
        group_id="notification-svc",
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        value_deserializer=lambda v: json.loads(v.decode("utf-8")) if v else None,
    )
    try:
        await consumer.start()
        log.info(
            "notification_consumer_connected",
            topic="api-call-events",
            brokers=settings.kafka_brokers,
        )
        async for msg in consumer:
            try:
                await process_event(msg.value or {})
            except Exception:
                log.exception("webhook_process_error")
            finally:
                with suppress(Exception):
                    await consumer.commit()
    except asyncio.CancelledError:
        pass
    except Exception:
        log.exception("notification_consumer_crashed")
    finally:
        with suppress(Exception):
            await consumer.stop()
