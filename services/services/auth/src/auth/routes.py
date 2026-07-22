"""auth 路由。

两类端点：
1. 内部（dispatcher 等服务调用）—— /v1/apikey/verify
   - 不走 APIKey middleware（它本身就是 APIKey middleware 依赖）
   - 仅靠 K8s NetworkPolicy 限制只能集群内访问
2. 用户/管理员（通过 dispatcher 调用）—— /v1/apps/{app_id}/api-keys 等
   - 走标准 APIKey middleware，自动注入 TenantContext
"""

import uuid

from apihub_core.apisix_client import upsert_consumer
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
    AppCreate,
    AppResponse,
    AuthResponse,
    ConsentResponse,
    ConsentWithdrawResponse,
    DeleteAccountResponse,
    ExportResponse,
    HmacSecretRequest,
    HmacSecretResponse,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    VerifyRequest,
    VerifyResponse,
)
from auth.repository import (
    create_api_key,
    create_app,
    get_hmac_secret_plaintext,
    get_tenant_home_region,
    list_api_keys_for_app,
    list_apps_for_tenant,
    revoke_api_key,
    rotate_hmac_secret,
    verify_api_key_record,
)

log = get_logger(__name__)


async def _inject_home_region_on_create(*, key_id: str, key: str, tenant_id: str) -> None:
    """create_key 的 testable seam：把 tenant.home_region 注入 consumer labels。

    多区写亲和（R3b S1-T3）：每个 API key 对应的 APISIX consumer 携带
    `labels={"home_region": <hr>}`，供 APISIX tenant-affinity 插件读取以决定写路由。
    tenant 无 home_region 时传 labels=None（upsert_consumer 视为不带 labels 字段）。
    """
    home_region = await get_tenant_home_region(tenant_id)  # type: ignore
    labels = {"home_region": home_region} if home_region else None
    await upsert_consumer(key_id=key_id, key=key, labels=labels)


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

        # 1+2. 缓存命中（正/负都走 get_cached；负缓存存 {"invalid": True}，
        #    与 /v1/apikey/verify 一致 —— 修正既有 cache API 误用，R0a §2.4）
        cached = await get_cached(payload.api_key)
        if cached is not None:
            if cached.get("invalid"):
                raise ApiError(ErrorCode.UNAUTHORIZED, "invalid api key")
            if "home_region" not in cached:
                cached["home_region"] = await get_tenant_home_region(cached["tenant_id"])
            return {**cached, "home_region": cached["home_region"]}

        # 3. DB 查
        record = await verify_api_key_record(payload.api_key)
        if not record:
            await cache_negative(payload.api_key)
            raise ApiError(ErrorCode.UNAUTHORIZED, "invalid api key")

        # 4. 写正缓存（带上 home_region，后续缓存命中无需额外 DB 查询）
        home_region = await get_tenant_home_region(record["tenant_id"])
        record_with_region = {**record, "home_region": home_region}
        await cache_positive(payload.api_key, record_with_region)
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
            signing=payload.signing,
        )

        # R1d：随 key 生命周期同步 APISIX consumer（edge 校验）+ 预热 Redis 身份缓存
        # （dispatcher 信任路径命中即不回源 auth）。best-effort：失败仅记日志，不回滚 key
        # （key 仍可用——dispatcher 回落 HTTP auth）。
        # R3b S1-T3：consumer labels 注入 tenant.home_region（多区写亲和 consumer 侧）。
        # R2e：identity 缓存带 key_id（cold-path /v1/internal/hmac-secret 入参）+
        #      hmac_enrolled；signing key 额外预热加密 secret 缓存（dispatcher 验签暖路径）。
        try:
            from apihub_core import identity

            from auth.apikey import POSITIVE_CACHE_TTL

            await _inject_home_region_on_create(
                key_id=key_id, key=plaintext, tenant_id=ctx.tenant_id
            )
            identity_payload = {
                "is_active": True,
                "tenant_id": ctx.tenant_id,
                "tenant_type": ctx.tenant_type,
                "app_id": app_id,
                "is_platform_admin": ctx.is_platform_admin,
                "scopes": payload.scopes,
                "expires_at": payload.expires_at.isoformat() if payload.expires_at else None,
                "key_id": key_id,  # R2e: cold path /v1/internal/hmac-secret 入参
                "hmac_enrolled": payload.signing,
            }
            await identity.write_identity(plaintext, identity_payload, ttl=POSITIVE_CACHE_TTL)
            if payload.signing and record.get("hmac_secret"):
                from apihub_core.crypto import encrypt_secret

                await identity.write_hmac_secret(
                    plaintext,
                    encrypt_secret(record["hmac_secret"]),
                    ttl=POSITIVE_CACHE_TTL,
                )
        except Exception:  # noqa: BLE001
            log.warning(
                "apisix_consumer_upsert_failed", key_id=key_id, app_id=app_id, exc_info=True
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

        # R1d：删 APISIX consumer（best-effort）
        try:
            from apihub_core import apisix_client

            await apisix_client.delete_consumer(key_id)
        except Exception:  # noqa: BLE001
            log.warning("apisix_consumer_delete_failed", key_id=key_id, exc_info=True)

        log.info(
            "apikey_revoked",
            key_id=key_id,
            app_id=revoked["app_id"],
            tenant_id=ctx.tenant_id,
        )
        return {"id": key_id, "status": "revoked"}

    # ========== HMAC 签名 secret 生命周期（R2e）==========

    @app.post("/v1/api-keys/{key_id}/hmac-secret/rotate")
    async def rotate_hmac(key_id: str):
        """轮换 HMAC secret → 新明文仅返回一次 + 失效 secret Redis 缓存（identity 不动）。

        identity 缓存保留（tenant_id/scopes/key_id/hmac_enrolled 不变），只清加密 secret
        缓存，使下一次签名请求走 cold path 重新取并回填新 secret。key_hash 仅内部用于
        失效缓存，不回传 client。
        """
        ctx = require_tenant()
        from apihub_core import redis

        result = await rotate_hmac_secret(key_id, ctx.tenant_id)
        # 失效 warm secret 缓存：hmac_secret_cache_key = "hmac_secret:" + sha256(明文 api_key)
        # = key_hash（rotate_hmac_secret RETURNING key_hash，与 identity.hmac_secret_cache_key 一致）。
        await redis.raw_client().delete("hmac_secret:" + result["key_hash"])
        log.info("hmac_secret_rotated", key_id=key_id, tenant_id=ctx.tenant_id)
        return {"key_id": result["key_id"], "hmac_secret": result["hmac_secret"]}

    @app.post("/v1/internal/hmac-secret", response_model=HmacSecretResponse)
    async def fetch_hmac_secret(payload: HmacSecretRequest):
        """dispatcher 冷路径取 HMAC secret 明文（集群内 + admin_db_session bypass RLS）。

        等价 /v1/apikey/verify 的冷回源。未 enrolled（列 NULL）→ hmac_secret=None（非 401；
        dispatcher 据此判定该 key 不走签名模式）。在 skip_auth_paths，靠 K8s NetworkPolicy
        限集群内来源（同 /v1/apikey/verify）。
        """
        secret = await get_hmac_secret_plaintext(payload.key_id)
        return HmacSecretResponse(hmac_secret=secret)

    # ========== app 管理（受保护端点；portal 转发用户 JWT 到此）==========

    @app.post("/v1/apps", response_model=AppResponse)
    async def create_app_route(payload: AppCreate):
        """创建 app。调用方 tenant 来自中间件注入的 ctx（JWT 或 APIKey）。"""
        ctx = require_tenant()
        app_id = f"app_{uuid.uuid4().hex[:16]}"
        record = await create_app(
            app_id=app_id,
            tenant_id=ctx.tenant_id,
            name=payload.name,
            app_type=payload.type,
        )
        log.info("app_created", app_id=app_id, tenant_id=ctx.tenant_id)
        return AppResponse(**record)

    @app.get("/v1/apps", response_model=list[AppResponse])
    async def list_apps_route():
        """列出本租户所有 app。"""
        ctx = require_tenant()
        rows = await list_apps_for_tenant(ctx.tenant_id)
        return [AppResponse(**r) for r in rows]

    # ========== 外部开发者身份端点（公开，skip APIKey middleware）==========

    @app.post("/v1/auth/register", status_code=201)
    async def register(payload: RegisterRequest):
        """外部开发者注册：写 pending user + 发验证 token（dev stub 存 Redis）。"""
        from auth import identity

        return await identity.create_user(
            email=payload.email,
            password=payload.password,
            phone=payload.phone,
            name=payload.name,
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

    @app.delete("/v1/auth/account", response_model=DeleteAccountResponse)
    async def delete_account():
        """删除当前账号（GDPR Right to erasure）。需 JWT。"""
        ctx = require_tenant()
        from auth import identity

        user_id = ctx.user_id
        if not user_id:
            raise ApiError(ErrorCode.UNAUTHORIZED, "JWT required", http_status=401)
        await identity.anonymize_user(user_id=user_id)
        return DeleteAccountResponse(user_id=user_id)

    @app.get("/v1/auth/account/export", response_model=ExportResponse)
    async def export_account():
        """导出个人数据（GDPR Right to portability）。需 JWT。"""
        ctx = require_tenant()
        from auth import identity

        user_id = ctx.user_id
        if not user_id:
            raise ApiError(ErrorCode.UNAUTHORIZED, "JWT required", http_status=401)
        return await identity.export_user_data(user_id=user_id)

    # ========== 同意管理 ==========

    @app.get("/v1/auth/consent", response_model=ConsentResponse)
    async def list_consents():
        """查询当前用户的同意记录。需 JWT。"""
        ctx = require_tenant()
        from auth import identity

        user_id = ctx.user_id
        if not user_id:
            raise ApiError(ErrorCode.UNAUTHORIZED, "JWT required", http_status=401)
        consents = await identity.list_consents(user_id=user_id)
        return ConsentResponse(consents=consents)

    @app.post("/v1/auth/consent/withdraw", response_model=ConsentWithdrawResponse)
    async def withdraw_consents():
        """撤回全部同意（触发账号匿名化）。需 JWT。"""
        ctx = require_tenant()
        from auth import identity

        user_id = ctx.user_id
        if not user_id:
            raise ApiError(ErrorCode.UNAUTHORIZED, "JWT required", http_status=401)
        await identity.withdraw_consent(user_id=user_id)
        return ConsentWithdrawResponse(user_id=user_id)

    @app.get("/v1/auth/health")
    async def health():
        return {"status": "ok", "service": "auth"}
