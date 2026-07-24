"""portal-bff 路由 —— 身份端点转发 auth + app/key 自助。"""

import httpx
from apihub_core.config import get_settings
from apihub_core.errors import ApiError, ErrorCode
from apihub_core.logging import get_logger
from apihub_core.tenant import require_tenant
from fastapi import FastAPI, Request

from portal import repository
from portal.models import (
    ApiKeyCreate,
    ApiKeyResponse,
    AppCreate,
    AppResponse,
    PlanInfo,
    SubscribeRequest,
    TryRequest,
)

log = get_logger(__name__)


def register_routes(app: FastAPI) -> None:
    settings = get_settings()
    # auth_service_url 形如 http://auth.apihub-system/v1/apikey/verify → 砍到 base
    # rsplit("/",3) 去掉 /v1/apikey/verify 三段，得 http://auth.apihub-system（无 /v1），
    # 否则拼 /v1/auth/login 会变成 /v1/v1/auth/login（双 /v1/）。
    auth_base = settings.auth_service_url.rsplit("/", 3)[0]

    async def _forward(method: str, path: str, **kw) -> tuple[int, dict]:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.request(method, f"{auth_base}{path}", **kw)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {"raw": r.text[:200]}

    # ========== 身份端点（转发 auth，无需 JWT）==========
    @app.post("/v1/portal/auth/register", status_code=201)
    async def register(payload: dict):
        st, body = await _forward("POST", "/v1/auth/register", json=payload)
        if st >= 400:
            raise ApiError(ErrorCode.INTERNAL, f"auth error: {body}", http_status=st)
        return body

    @app.get("/v1/portal/auth/verify-email")
    async def verify_email(token: str):
        st, body = await _forward("GET", "/v1/auth/verify-email", params={"token": token})
        if st >= 400:
            raise ApiError(ErrorCode.INTERNAL, f"auth error: {body}", http_status=st)
        return body

    @app.post("/v1/portal/auth/login")
    async def login(payload: dict):
        st, body = await _forward("POST", "/v1/auth/login", json=payload)
        if st >= 400:
            raise ApiError(ErrorCode.UNAUTHORIZED, "invalid credentials", http_status=st)
        return body

    @app.post("/v1/portal/auth/refresh")
    async def portal_refresh(payload: dict):
        st, body = await _forward("POST", "/v1/auth/refresh", json=payload)
        if st >= 400:
            raise ApiError(ErrorCode.UNAUTHORIZED, "refresh failed", http_status=st)
        return body

    @app.delete("/v1/portal/auth/account")
    async def portal_delete_account(request: Request):
        """删除账号（需 JWT）。转发到 auth-svc。"""
        require_tenant()
        st, body = await _forward(
            "DELETE",
            "/v1/auth/account",
            headers={"Authorization": request.headers.get("Authorization", "")},
        )
        if st >= 400:
            raise ApiError(ErrorCode.INTERNAL, body, http_status=st)  # type: ignore
        return body

    @app.get("/v1/portal/auth/account/export")
    async def portal_export_account(request: Request):
        """导出个人数据（需 JWT）。转发到 auth-svc。"""
        require_tenant()
        st, body = await _forward(
            "GET",
            "/v1/auth/account/export",
            headers={"Authorization": request.headers.get("Authorization", "")},
        )
        if st >= 400:
            raise ApiError(ErrorCode.INTERNAL, body, http_status=st)  # type: ignore
        return body

    @app.get("/v1/portal/auth/consent")
    async def portal_list_consents(request: Request):
        """查询同意记录。转发到 auth-svc。"""
        require_tenant()
        st, body = await _forward(
            "GET",
            "/v1/auth/consent",
            headers={"Authorization": request.headers.get("Authorization", "")},
        )
        if st >= 400:
            raise ApiError(ErrorCode.INTERNAL, body, http_status=st)  # type: ignore
        return body

    @app.post("/v1/portal/auth/consent/withdraw")
    async def portal_withdraw_consents(request: Request):
        """撤回同意。转发到 auth-svc。"""
        require_tenant()
        st, body = await _forward(
            "POST",
            "/v1/auth/consent/withdraw",
            headers={"Authorization": request.headers.get("Authorization", "")},
        )
        if st >= 400:
            raise ApiError(ErrorCode.INTERNAL, body, http_status=st)  # type: ignore
        return body

    # ========== API 目录（需 JWT）==========
    @app.get("/v1/portal/apis")
    async def list_portal_apis(
        search: str = "",
        category: str = "",
        tag: str = "",
        limit: int = 50,
        offset: int = 0,
    ):
        """API 目录列表 + 搜索/过滤/分页。"""
        require_tenant()
        return await repository.list_portal_apis(
            search=search,
            category=category,
            tag=tag,
            limit=min(limit, 200),
            offset=offset,
        )

    @app.get("/v1/portal/apis/{api_id}")
    async def get_api_detail(api_id: str):
        """API 详情（含版本列表 + schema）。"""
        require_tenant()
        return await repository.get_api_detail(api_id)

    @app.post("/v1/portal/try")
    async def try_endpoint(payload: TryRequest):
        """在线调试代理（用 API Key 调通后端）。"""
        require_tenant()
        return await repository.try_api(payload)

    # ========== 用量/计费（需 JWT）==========
    @app.get("/v1/portal/usage")
    async def portal_usage():
        """Portal 用量概览（当月调用量+剩余+plan）。"""
        ctx = require_tenant()
        return await repository.get_billing_summary(ctx.tenant_id)

    @app.get("/v1/portal/plans", response_model=list[PlanInfo])
    async def portal_plans():
        """Plan 列表（对比）。"""
        require_tenant()
        return await repository.list_plans()

    @app.get("/v1/portal/subscription")
    async def portal_subscription():
        """当前 Plan + 周期。"""
        ctx = require_tenant()
        sub = await repository.get_subscription(ctx.tenant_id)
        return sub if sub else {"plan_code": "free", "plan_name": "Free", "status": "active"}

    @app.post("/v1/portal/subscribe")
    async def portal_subscribe(payload: SubscribeRequest):
        """变更 Plan（admin 直写 subscription 表）。"""
        ctx = require_tenant()
        return await repository.subscribe_plan(ctx.tenant_id, payload.plan_code)

    @app.get("/v1/portal/invoices")
    async def portal_invoices(limit: int = 12, offset: int = 0):
        """账单记录列表（分页）。"""
        ctx = require_tenant()
        return await repository.get_invoices(ctx.tenant_id, limit, offset)

    # ========== Webhook 管理（需 JWT）==========
    notif_base = settings.notification_service_url

    @app.get("/v1/portal/webhooks")
    async def portal_list_webhooks():
        require_tenant()
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(f"{notif_base}/webhooks")
        return r.json()

    @app.post("/v1/portal/webhooks", status_code=201)
    async def portal_create_webhook(payload: dict):
        require_tenant()
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.post(f"{notif_base}/webhooks", json=payload)
        return r.json()

    @app.put("/v1/portal/webhooks/{webhook_id}")
    async def portal_update_webhook(webhook_id: str, payload: dict):
        require_tenant()
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.put(f"{notif_base}/webhooks/{webhook_id}", json=payload)
        return r.json()

    @app.delete("/v1/portal/webhooks/{webhook_id}")
    async def portal_delete_webhook(webhook_id: str):
        require_tenant()
        async with httpx.AsyncClient(timeout=5.0) as c:
            await c.delete(f"{notif_base}/webhooks/{webhook_id}")
        return {"status": "deleted"}

    @app.post("/v1/portal/webhooks/{webhook_id}/test")
    async def portal_test_webhook(webhook_id: str):
        require_tenant()
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.post(f"{notif_base}/webhooks/{webhook_id}/test")
        return r.json()

    # ========== 高级分析（需 JWT → 转发 trace-svc，§9-B 护栏：前端不得直连 trace）==========
    trace_base = settings.trace_service_url

    @app.get("/v1/portal/analytics/funnel")
    async def portal_funnel(request: Request):
        """调用漏斗 —— 薄转发 trace-svc，透传用户 JWT（trace-svc 据其做租户隔离）。"""
        require_tenant()
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(
                f"{trace_base}/analytics/funnel",
                headers={"Authorization": request.headers.get("Authorization", "")},
                params=dict(request.query_params),
            )
        return r.json()

    @app.get("/v1/portal/analytics/co-occurrence")
    async def portal_cooccurrence(request: Request):
        """API 共现 —— 薄转发 trace-svc。"""
        require_tenant()
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(
                f"{trace_base}/analytics/co-occurrence",
                headers={"Authorization": request.headers.get("Authorization", "")},
                params=dict(request.query_params),
            )
        return r.json()

    # ========== app/key 自助（需 JWT → require_tenant）==========
    @app.post("/v1/portal/apps", response_model=AppResponse, status_code=201)
    async def create_app(request: Request, payload: AppCreate):
        """建 app —— 转发用户 JWT 到 auth /v1/apps（不再直写 app 表）。"""
        require_tenant()
        st, body = await _forward(
            "POST",
            "/v1/apps",
            headers={"Authorization": request.headers.get("Authorization", "")},
            json={"name": payload.name, "type": payload.type},
        )
        if st >= 400:
            raise ApiError(ErrorCode.INTERNAL, f"auth error: {body}", http_status=st)
        return AppResponse(**body)

    @app.get("/v1/portal/apps", response_model=list[AppResponse])
    async def list_apps(request: Request):
        """列本租户 app —— 转发 auth /v1/apps。"""
        require_tenant()
        st, body = await _forward(
            "GET",
            "/v1/apps",
            headers={"Authorization": request.headers.get("Authorization", "")},
        )
        if st >= 400:
            raise ApiError(ErrorCode.INTERNAL, f"auth error: {body}", http_status=st)
        return [AppResponse(**a) for a in body]

    @app.post(
        "/v1/portal/apps/{app_id}/api-keys",
        response_model=ApiKeyResponse,
        status_code=201,
    )
    async def create_api_key(request: Request, app_id: str, payload: ApiKeyCreate):
        """建 APIKey —— 转发 auth，明文 key 仅此次返回。"""
        require_tenant()
        st, body = await _forward(
            "POST",
            f"/v1/apps/{app_id}/api-keys",
            headers={"Authorization": request.headers.get("Authorization", "")},
            json={"name": payload.name},
        )
        if st >= 400:
            raise ApiError(ErrorCode.INTERNAL, f"auth error: {body}", http_status=st)
        return ApiKeyResponse(
            id=body["id"],
            app_id=body["app_id"],
            name=body["name"],
            key_prefix=body["display_prefix"],  # 映射 auth 字段
            api_key=body["api_key"],
        )
