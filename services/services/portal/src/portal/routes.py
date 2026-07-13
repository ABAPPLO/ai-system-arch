"""portal-bff 路由 —— 身份端点转发 auth + app/key 自助。"""

import httpx
from apihub_core.config import get_settings
from apihub_core.errors import ApiError, ErrorCode
from apihub_core.logging import get_logger
from apihub_core.tenant import require_tenant
from fastapi import FastAPI

from portal import repository
from portal.models import ApiKeyCreate, ApiKeyResponse, AppCreate, AppResponse

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
            raise ApiError(
                ErrorCode.INTERNAL, f"auth error: {body}", http_status=st
            )
        return body

    @app.get("/v1/portal/auth/verify-email")
    async def verify_email(token: str):
        st, body = await _forward(
            "GET", "/v1/auth/verify-email", params={"token": token}
        )
        if st >= 400:
            raise ApiError(
                ErrorCode.INTERNAL, f"auth error: {body}", http_status=st
            )
        return body

    @app.post("/v1/portal/auth/login")
    async def login(payload: dict):
        st, body = await _forward("POST", "/v1/auth/login", json=payload)
        if st >= 400:
            raise ApiError(
                ErrorCode.UNAUTHORIZED, "invalid credentials", http_status=st
            )
        return body

    # ========== app/key 自助（需 JWT → require_tenant）==========
    @app.post("/v1/portal/apps", response_model=AppResponse, status_code=201)
    async def create_app(payload: AppCreate):
        ctx = require_tenant()
        return await repository.create_app_for_user(
            tenant_id=ctx.tenant_id, name=payload.name, app_type=payload.type
        )

    @app.get("/v1/portal/apps", response_model=list[AppResponse])
    async def list_apps():
        ctx = require_tenant()
        return await repository.list_apps_for_user(tenant_id=ctx.tenant_id)

    @app.post(
        "/v1/portal/apps/{app_id}/api-keys",
        response_model=ApiKeyResponse,
        status_code=201,
    )
    async def create_api_key(app_id: str, payload: ApiKeyCreate):
        ctx = require_tenant()
        return await repository.create_api_key_for_app(
            tenant_id=ctx.tenant_id, app_id=app_id, name=payload.name
        )
