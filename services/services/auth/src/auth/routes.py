"""auth 路由。

两类端点：
1. 内部（dispatcher 等服务调用）—— /v1/apikey/verify
   - 不走 APIKey middleware（它本身就是 APIKey middleware 依赖）
   - 仅靠 K8s NetworkPolicy 限制只能集群内访问
2. 用户/管理员（通过 dispatcher 调用）—— /v1/apps/{app_id}/api-keys 等
   - 走标准 APIKey middleware，自动注入 TenantContext
"""

import uuid

from apihub_core.errors import ApiError, ErrorCode
from apihub_core.logging import get_logger
from apihub_core.tenant import require_tenant
from fastapi import FastAPI

from auth.apikey import generate_api_key, is_valid_format
from auth.cache import cache_negative, cache_positive, get_cached, invalidate
from auth.models import (
    ApiKeyCreate,
    ApiKeyListItem,
    ApiKeyResponse,
    AuthResponse,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    VerifyRequest,
    VerifyResponse,
)
from auth.repository import (
    create_api_key,
    get_tenant_home_region,
    list_api_keys_for_app,
    revoke_api_key,
    verify_api_key_record,
)

log = get_logger(__name__)


def register_routes(app: FastAPI) -> None:
    # ========== 内部端点 ==========

    @app.post("/v1/apikey/verify", response_model=VerifyResponse)
    async def verify(payload: VerifyRequest):
        """APIKey 校验 —— 由 dispatcher / 其他内部服务调用。

        流程：
          1. 快速格式校验（拒绝明显垃圾请求）
          2. Redis 缓存查
          3. PG 跨租户查（admin_db_session）
          4. 缓存结果（正 5min / 负 1min）
        """
        if not is_valid_format(payload.api_key):
            raise ApiError(ErrorCode.UNAUTHORIZED, "invalid api key format")

        # 1. 缓存命中
        cached = await get_cached(payload.api_key)
        if cached is not None:
            if cached.get("invalid"):
                # 负缓存
                raise ApiError(ErrorCode.UNAUTHORIZED, "invalid api key")
            return VerifyResponse(**cached)

        # 2. DB 查
        record = await verify_api_key_record(payload.api_key)
        if not record:
            await cache_negative(payload.api_key)
            raise ApiError(ErrorCode.UNAUTHORIZED, "invalid api key")

        # 3. 正缓存
        await cache_positive(payload.api_key, record)
        log.info(
            "apikey_verified",
            app_id=record["app_id"],
            tenant_id=record["tenant_id"],
        )
        return VerifyResponse(**record)

    @app.post("/internal/auth/check")
    async def auth_check(payload: VerifyRequest):
        """APISIX 租户亲和检查 —— 返回 key 数据 + home_region。

        APISIX tenant-affinity 插件在认证后调用此端点获取 consumer 的 home_region，
        用于决定是否将写请求重定向到租户归属 Region。

        缓存策略（与 /v1/apikey/verify 一致）：
          1. 负缓存查（拒绝已失效 key）
          2. 正缓存查（Redis 5min TTL）
          3. DB 查 + 写正/负缓存
        """
        if not is_valid_format(payload.api_key):
            raise ApiError(ErrorCode.UNAUTHORIZED, "invalid api key format")

        # 1. 负缓存命中
        if await cache_negative.exists(payload.api_key):
            raise ApiError(ErrorCode.UNAUTHORIZED, "invalid api key")

        # 2. 正缓存命中
        cached = await cache_positive.get(payload.api_key)
        if cached:
            if "home_region" not in cached:
                cached["home_region"] = await get_tenant_home_region(cached["tenant_id"])
            return {**cached, "home_region": cached["home_region"]}

        # 3. DB 查
        record = await verify_api_key_record(payload.api_key)
        if not record:
            await cache_negative.set(payload.api_key)
            raise ApiError(ErrorCode.UNAUTHORIZED, "invalid api key")

        # 4. 写正缓存（带上 home_region，后续缓存命中无需额外 DB 查询）
        home_region = await get_tenant_home_region(record["tenant_id"])
        record_with_region = {**record, "home_region": home_region}
        await cache_positive.set(payload.api_key, record_with_region)
        log.info(
            "auth_check_verified",
            app_id=record["app_id"],
            tenant_id=record["tenant_id"],
        )
        return record_with_region

    # ========== 用户端点（走 dispatcher + 标准 APIKey middleware） ==========

    @app.post("/v1/apps/{app_id}/api-keys", response_model=ApiKeyResponse)
    async def create_key(app_id: str, payload: ApiKeyCreate):
        """为指定 app 创建新 APIKey。

        鉴权：调用方的 tenant_id 必须等于 app 的 tenant_id（RLS 自动校验）。
        明文 api_key 只在此响应中返回一次。
        """
        ctx = require_tenant()
        plaintext, key_hash, display_prefix = generate_api_key()
        key_id = f"key_{uuid.uuid4().hex[:16]}"

        record = await create_api_key(
            key_id=key_id,
            app_id=app_id,
            tenant_id=ctx.tenant_id,
            name=payload.name,
            key_hash=key_hash,
            display_prefix=display_prefix,
            scopes=payload.scopes,
            expires_at=payload.expires_at,
        )

        log.info(
            "apikey_created",
            key_id=key_id,
            app_id=app_id,
            tenant_id=ctx.tenant_id,
        )

        return ApiKeyResponse(
            **record,
            api_key=plaintext,
        )

    @app.get("/v1/apps/{app_id}/api-keys", response_model=list[ApiKeyListItem])
    async def list_keys(app_id: str):
        """列出 app 的所有 APIKey（不含明文）。"""
        rows = await list_api_keys_for_app(app_id)
        return [ApiKeyListItem(**r) for r in rows]

    @app.delete("/v1/api-keys/{key_id}")
    async def revoke_key(key_id: str):
        """吊销 APIKey。

        同租户 RLS 校验 + 主动清缓存。
        """
        ctx = require_tenant()
        revoked = await revoke_api_key(key_id)

        # 清缓存（用 key_hash）
        await invalidate(revoked["key_hash"])

        log.info(
            "apikey_revoked",
            key_id=key_id,
            app_id=revoked["app_id"],
            tenant_id=ctx.tenant_id,
        )
        return {"id": key_id, "status": "revoked"}

    # ========== 外部开发者身份端点（公开，skip APIKey middleware）==========

    @app.post("/v1/auth/register", status_code=201)
    async def register(payload: RegisterRequest):
        """外部开发者注册：写 pending user + 发验证 token（dev stub 存 Redis）。"""
        from auth import identity

        return await identity.create_user(
            email=payload.email, password=payload.password,
            phone=payload.phone, name=payload.name,
        )

    @app.get("/v1/auth/verify-email")
    async def verify_email_endpoint(token: str):
        """邮箱验证：激活 user + 加入 external-public 租户。"""
        from auth import identity

        return await identity.verify_email(token)

    @app.post("/v1/auth/login", response_model=AuthResponse)
    async def login_endpoint(payload: LoginRequest):
        """登录：bcrypt 校验 + status 检查 → 签 JWT + refresh token。"""
        from auth import identity

        return await identity.login(email=payload.email, password=payload.password)

    @app.post("/v1/auth/refresh")
    async def refresh_endpoint(payload: RefreshRequest):
        """刷新 access token（rotation）。"""
        from auth import identity

        return await identity.refresh_access(payload.refresh_token)

    @app.get("/v1/auth/health")
    async def health():
        return {"status": "ok", "service": "auth"}
