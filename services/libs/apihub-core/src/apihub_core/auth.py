"""鉴权中间件 —— APIKey / JWT 校验。

校验成功后，把 TenantContext 注入到 contextvars，
下游 db_session / redis / kafka 自动感知租户。

详见 docs/08-observability-security.md §7
"""

import httpx
from fastapi import Request

from apihub_core.config import Settings
from apihub_core.errors import ApiError, ErrorCode
from apihub_core.tenant import TenantContext, set_tenant_context


async def authenticate_request(
    request: Request,
    settings: Settings,
    api_key: str,
    required_scopes: list[str] | None = None,
) -> TenantContext:
    """通过 auth 服务校验 APIKey，回填 TenantContext。

    auth 服务（docs/03-services.md §3.3）维护 ak -> app + tenant 映射，
    Redis 缓存热点查询。
    """
    if not api_key:
        raise ApiError(ErrorCode.UNAUTHORIZED, "Missing API Key")

    # 缓存查询（生产环境强烈推荐）：`ak:{sha256(api_key)}` -> json
    # 这里直接调 auth 服务
    # timeout 5s：auth verify 可能回源 PG（cache-miss），2s 在依赖冷启动/抖动时偏紧，
    # 曾稳定触发 503 "Auth service unreachable"（见 db.init_pool 的 pool 预热注释）。
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            resp = await client.post(
                settings.auth_service_url,
                json={"api_key": api_key},
                headers={"X-Internal-Service": settings.app_name},
            )
        except httpx.RequestError as e:
            raise ApiError(
                ErrorCode.INTERNAL,
                # 带异常类型 + repr：httpx 超时类异常 str 常为空串，否则日志里只剩 "unreachable: " 无法定位。
                f"Auth service unreachable: {type(e).__name__}: {e!r}",
                http_status=503,
            ) from e

    if resp.status_code == 404:
        raise ApiError(ErrorCode.UNAUTHORIZED, "Invalid API Key")
    if resp.status_code != 200:
        raise ApiError(ErrorCode.UNAUTHORIZED, "API Key verify failed")

    # auth-svc 直接返回 VerifyResponse（无 envelope）：
    #   {is_active, tenant_id, tenant_type, app_id, is_platform_admin, scopes, expires_at}
    data = resp.json()
    if not data.get("is_active"):
        raise ApiError(ErrorCode.UNAUTHORIZED, "API Key disabled")

    ctx = TenantContext(
        tenant_id=data["tenant_id"],
        tenant_type=data["tenant_type"],
        app_id=data["app_id"],
        is_platform_admin=data.get("is_platform_admin", False),
    )
    set_tenant_context(ctx)
    return ctx
