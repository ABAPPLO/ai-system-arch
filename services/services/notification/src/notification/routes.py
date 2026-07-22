"""notification 路由 —— Webhook CRUD + 测试。"""

from apihub_core.errors import ApiError, ErrorCode
from apihub_core.tenant import require_tenant
from fastapi import FastAPI

from notification import channels, repository
from notification.models import (
    ChannelConfigCreate,
    ChannelConfigUpdate,
    NotifyRequest,
    WebhookCreate,
    WebhookTestResult,
    WebhookUpdate,
)


async def _test_webhook(url: str, secret: str | None) -> WebhookTestResult:
    """发送测试事件到 Webhook URL。"""
    import time

    import httpx

    try:
        start = time.perf_counter()
        async with httpx.AsyncClient(timeout=10.0) as c:
            resp = await c.post(
                url,
                json={"event": "test", "data": {"message": "Hello from APIHub"}},
                headers={"X-Webhook-Secret": secret or ""},
            )
        elapsed = int((time.perf_counter() - start) * 1000)
        return WebhookTestResult(
            success=resp.status_code < 500, status_code=resp.status_code, latency_ms=elapsed
        )
    except httpx.RequestError as e:
        return WebhookTestResult(success=False, error=str(e))


def register_routes(app: FastAPI) -> None:
    @app.get("/v1/notification/webhooks")
    async def list_webhooks():
        ctx = require_tenant()
        return await repository.list_webhooks(tenant_id=ctx.tenant_id)

    @app.post("/v1/notification/webhooks", status_code=201)
    async def create_webhook(payload: WebhookCreate):
        ctx = require_tenant()
        return await repository.create_webhook(
            tenant_id=ctx.tenant_id,
            url=payload.url,
            events=payload.events,
            secret=payload.secret,
        )

    @app.put("/v1/notification/webhooks/{webhook_id}")
    async def update_webhook(webhook_id: str, payload: WebhookUpdate):
        ctx = require_tenant()
        updates = payload.model_dump(exclude_none=True)
        if not updates:
            raise ApiError(ErrorCode.INVALID_INPUT, "no fields to update", http_status=400)
        return await repository.update_webhook(
            tenant_id=ctx.tenant_id,
            webhook_id=webhook_id,
            updates=updates,
        )

    @app.delete("/v1/notification/webhooks/{webhook_id}")
    async def delete_webhook(webhook_id: str):
        ctx = require_tenant()
        await repository.delete_webhook(tenant_id=ctx.tenant_id, webhook_id=webhook_id)
        return {"status": "deleted"}

    @app.post("/v1/notification/webhooks/{webhook_id}/test")
    async def test_webhook(webhook_id: str):
        ctx = require_tenant()
        hooks = await repository.list_webhooks(tenant_id=ctx.tenant_id)
        hook = next((h for h in hooks if h["id"] == webhook_id), None)
        if not hook:
            raise ApiError(ErrorCode.NOT_FOUND, "webhook not found")
        return await _test_webhook(hook["url"], hook.get("secret"))

    # ===== /v1/notification/channel-configs（per-tenant CRUD）=====
    @app.get("/v1/notification/channel-configs")
    async def list_channel_configs():
        ctx = require_tenant()
        return await repository.list_channel_configs(tenant_id=ctx.tenant_id)

    @app.post("/v1/notification/channel-configs", status_code=201)
    async def create_channel_config(payload: ChannelConfigCreate):
        ctx = require_tenant()
        return await repository.create_channel_config(
            tenant_id=ctx.tenant_id,
            channel_type=payload.channel_type,
            name=payload.name,
            config=payload.config,
            status=payload.status,
        )

    @app.put("/v1/notification/channel-configs/{config_id}")
    async def update_channel_config(config_id: str, payload: ChannelConfigUpdate):
        ctx = require_tenant()
        return await repository.update_channel_config(
            tenant_id=ctx.tenant_id,
            config_id=config_id,
            updates=payload.model_dump(exclude_none=True),
        )

    @app.delete("/v1/notification/channel-configs/{config_id}")
    async def delete_channel_config(config_id: str):
        ctx = require_tenant()
        await repository.delete_channel_config(tenant_id=ctx.tenant_id, config_id=config_id)
        return {"status": "deleted"}

    # ===== /v1/internal/notify/send + /batch =====
    async def _handle_one(tenant_id: str, req: NotifyRequest) -> dict:
        config = await repository.get_active_channel_config(
            tenant_id=tenant_id, channel_type=req.channel_type
        )
        subject, body = await repository.render_template(
            code=req.template_code,
            channel_type=req.channel_type,
            variables=req.variables,
            locale=req.locale,
        )
        channel = channels.get(req.channel_type)
        result = await channel.send(
            channels.NotificationMessage(
                recipient=req.recipient,
                subject=subject,
                body=body,
                channel_type=req.channel_type,
                config=config or {},
                meta={"template_code": req.template_code},
            )
        )
        await repository.insert_notification_log(
            tenant_id=tenant_id,
            template_code=req.template_code,
            channel_type=req.channel_type,
            recipient=req.recipient,
            status="sent" if result.success else "failed",
            error=result.error or "",
            provider_msg_id=result.provider_msg_id or "",
        )
        return {
            "success": result.success,
            "error": result.error,
            "provider_msg_id": result.provider_msg_id,
        }

    @app.post("/v1/internal/notify/send")
    async def notify_send(payload: NotifyRequest):
        ctx = require_tenant()
        return await _handle_one(ctx.tenant_id, payload)

    @app.post("/v1/internal/notify/batch")
    async def notify_batch(payload: list[NotifyRequest]):
        import asyncio

        ctx = require_tenant()
        results = await asyncio.gather(
            *[_handle_one(ctx.tenant_id, r) for r in payload], return_exceptions=True
        )
        out = []
        for r in results:
            if isinstance(r, BaseException):
                out.append({"success": False, "error": str(r), "provider_msg_id": None})
            else:
                out.append(r)
        return out

    @app.get("/v1/notification/health")
    async def health():
        return {"status": "ok", "service": "notification"}
