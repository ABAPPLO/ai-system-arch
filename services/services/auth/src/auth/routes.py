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
    VerifyRequest,
    VerifyResponse,
)
from auth.repository import (
    create_api_key,
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

    @app.get("/v1/auth/health")
    async def health():
        return {"status": "ok", "service": "auth"}
