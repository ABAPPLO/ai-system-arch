"""notification 路由 —— Webhook CRUD + 测试。"""

from apihub_core.errors import ApiError, ErrorCode
from apihub_core.tenant import require_tenant
from fastapi import FastAPI

from notification import repository
from notification.models import WebhookCreate, WebhookTestResult, WebhookUpdate


async def _test_webhook(url: str, secret: str | None) -> WebhookTestResult:
    """发送测试事件到 Webhook URL。"""
    import time
    import httpx
    try:
        start = time.perf_counter()
        async with httpx.AsyncClient(timeout=10.0) as c:
            resp = await c.post(url, json={"event": "test", "data": {"message": "Hello from APIHub"}},
                                headers={"X-Webhook-Secret": secret or ""})
        elapsed = int((time.perf_counter() - start) * 1000)
        return WebhookTestResult(success=resp.status_code < 500, status_code=resp.status_code, latency_ms=elapsed)
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
            tenant_id=ctx.tenant_id, url=payload.url,
            events=payload.events, secret=payload.secret,
        )

    @app.put("/v1/notification/webhooks/{webhook_id}")
    async def update_webhook(webhook_id: str, payload: WebhookUpdate):
        ctx = require_tenant()
        updates = payload.model_dump(exclude_none=True)
        if not updates:
            raise ApiError(ErrorCode.INVALID_INPUT, "no fields to update", http_status=400)
        return await repository.update_webhook(
            tenant_id=ctx.tenant_id, webhook_id=webhook_id, updates=updates,
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

    @app.get("/v1/notification/health")
    async def health():
        return {"status": "ok", "service": "notification"}
