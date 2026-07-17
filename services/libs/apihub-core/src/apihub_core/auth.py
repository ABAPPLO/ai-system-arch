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

    # 可信入口快速路径（R1d）：经 APISIX 入口的请求带 X-Ingress-Auth=<ingress_shared_secret>。
    # APISIX key-auth 已校验 key，本地读 auth 的 Redis 身份缓存回填 ctx，跳过 HTTP auth 回源
    # （消除 dispatcher→auth 冷启动 503）。安全前提：dispatcher 仅经 APISIX 可达（见 docs）。
    if (
        settings.ingress_shared_secret
        and request.headers.get("X-Ingress-Auth") == settings.ingress_shared_secret
    ):
        from apihub_core import identity

        cached = await identity.read_identity(api_key)
        if cached is not None:
            if cached.get("invalid") or not cached.get("is_active"):
                raise ApiError(ErrorCode.UNAUTHORIZED, "Invalid API Key")
            ctx = TenantContext(
                tenant_id=cached["tenant_id"],
                tenant_type=cached.get("tenant_type", "internal"),
                app_id=cached.get("app_id"),
                is_platform_admin=cached.get("is_platform_admin", False),
            )
            set_tenant_context(ctx)
            return ctx
        # miss → 回落下方 HTTP auth（会预热缓存）

    # JWT 分流：外部开发者「人」的 token（eyJ 开头）本地验签，
    # 不走 auth /v1/apikey/verify（那是机器 API Key 流程）。
    from apihub_core import jwt_utils

    if jwt_utils.is_jwt(api_key):
        try:
            payload = jwt_utils.decode_token(api_key, settings.jwt_secret)
        except jwt_utils.JWTError:
            raise ApiError(ErrorCode.UNAUTHORIZED, "invalid or expired token")
        ctx = TenantContext(
            tenant_id=payload["tenant_id"],
            tenant_type="external",
            user_id=payload["user_id"],
            is_platform_admin=payload.get("is_platform_admin", False),
        )
        set_tenant_context(ctx)
        return ctx
    # 否则：原 API Key 流程（以下 httpx 调 auth verify 代码不变）

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
