"""鉴权中间件 —— APIKey / JWT 校验。

校验成功后，把 TenantContext 注入到 contextvars，
下游 db_session / redis / kafka 自动感知租户。

详见 docs/08-observability-security.md §7
"""

import hashlib
import secrets
import time

import httpx
from fastapi import Request

from apihub_core.config import Settings
from apihub_core.errors import ApiError, ErrorCode
from apihub_core.tenant import TenantContext, set_tenant_context

_auth_httpx_client: httpx.AsyncClient | None = None


def _get_auth_httpx_client() -> httpx.AsyncClient:
    """进程级共享 httpx client（冷路径连接复用）。lazy 单例，遇 closed 重建。"""
    global _auth_httpx_client
    if _auth_httpx_client is None or _auth_httpx_client.is_closed:
        _auth_httpx_client = httpx.AsyncClient(timeout=5.0)
    return _auth_httpx_client


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

    # HMAC 签名分流（R2e）：带 X-App-Key 的请求走 in-app 验签（bearer 用 X-API-Key/
    # Authorization，无冲突）。只看 X-App-Key 存在（不看 X-Signature）—— enrolled key
    # 不带签名也要进 _verify_hmac 拒掉（防降级绕过 bearer）。JWT（eyJ 开头）优先，不进 HMAC 分流。
    from apihub_core import jwt_utils

    if not jwt_utils.is_jwt(api_key) and request.headers.get("X-App-Key"):
        return await _verify_hmac(request, settings, api_key)

    # 可信入口快速路径（R1d）：经 APISIX 入口的请求带 X-Ingress-Auth=<ingress_shared_secret>。
    # APISIX key-auth 已校验 key，本地读 auth 的 Redis 身份缓存回填 ctx，跳过 HTTP auth 回源
    # （消除 dispatcher→auth 冷启动 503）。安全前提：dispatcher 仅经 APISIX 可达（见 docs）。
    if (
        settings.ingress_shared_secret
        and secrets.compare_digest(
            request.headers.get("X-Ingress-Auth") or "", settings.ingress_shared_secret
        )
    ):
        from apihub_core import identity

        cached = await identity.read_identity(api_key)
        if cached is not None:
            if cached.get("invalid") or not cached.get("is_active"):
                raise ApiError(ErrorCode.UNAUTHORIZED, "Invalid API Key")
            # R2e C2: enrolled key 不可走 bearer 快路径（须带 X-App-Key 走 _verify_hmac 验签）。
            if cached.get("hmac_enrolled"):
                raise ApiError(ErrorCode.UNAUTHORIZED, "hmac signing required for this key")
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
    if jwt_utils.is_jwt(api_key):
        try:
            payload = jwt_utils.decode_token(api_key, settings.jwt_secret)
        except jwt_utils.JWTError:
            raise ApiError(ErrorCode.UNAUTHORIZED, "invalid or expired token") from None
        ctx = TenantContext(
            tenant_id=payload["tenant_id"],
            tenant_type="external",
            user_id=payload["user_id"],
            is_platform_admin=payload.get("is_platform_admin", False),
        )
        set_tenant_context(ctx)
        return ctx
    # 否则：API Key 流程 —— HTTP 回源 auth /v1/apikey/verify（cache-miss 时）。
    data = await _verify_via_auth_service(api_key, settings)
    # R2e C2: enrolled key 不可走 bearer（防泄漏的 key 绕过验签）—— 须带 X-App-Key 走 _verify_hmac。
    if data.get("hmac_enrolled"):
        raise ApiError(ErrorCode.UNAUTHORIZED, "hmac signing required for this key")

    ctx = TenantContext(
        tenant_id=data["tenant_id"],
        tenant_type=data["tenant_type"],
        app_id=data["app_id"],
        is_platform_admin=data.get("is_platform_admin", False),
    )
    set_tenant_context(ctx)
    return ctx


async def _verify_via_auth_service(api_key: str, settings: Settings) -> dict:
    """HTTP 回源 auth /v1/apikey/verify → 返回 VerifyResponse dict（auth 侧已暖 identity 缓存）。

    bearer 路径与 _verify_hmac 的 identity 冷回源（C4）共用：成功返回即意味着 identity 缓存
    已被 auth 服务端 cache_positive 暖好，caller 再 read_identity 即得。失败抛 401/503。
    """
    client = _get_auth_httpx_client()
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

    data = resp.json()
    if not data.get("is_active"):
        raise ApiError(ErrorCode.UNAUTHORIZED, "API Key disabled")
    return data


async def _verify_hmac(request: Request, settings: Settings, api_key: str) -> TenantContext:
    """in-app HMAC 验签（R2e）。

    1. identity 缓存取 ctx + hmac_enrolled（invalid/inactive → 401）
    2. enrolled 校验：enrolled 不带签名 / 未 enrolled 带签名 → 401（防降级绕过 bearer）
    3. 取 secret：warm=Redis 加密 blob+decrypt；cold=auth /v1/internal/hmac-secret
    4. timestamp ±window（±300s）；nonce SETNX TTL 防重放；verify compare_digest

    返回 TenantContext（同 authenticate_request），dispatcher 信任路径直接消费。
    """
    from apihub_core import crypto as crypto_mod
    from apihub_core import identity, signing
    from apihub_core.redis import raw_client

    cached, secret_blob = await identity.read_identity_and_hmac_secret(api_key)
    if cached is None:
        # C4: identity miss（HMAC-only key 长闲过期 / 冷启动）→ 回源 auth verify 暖缓存再续。
        # 否则 create_key 写的 identity（TTL 300s）过期后 HMAC key 自砖、client 无法自愈。
        await _verify_via_auth_service(api_key, settings)  # 失败抛 401/503；成功即暖 identity
        cached, secret_blob = await identity.read_identity_and_hmac_secret(api_key)
    if cached is None or cached.get("invalid"):
        raise ApiError(ErrorCode.UNAUTHORIZED, "invalid api key")
    if not cached.get("is_active"):
        raise ApiError(ErrorCode.UNAUTHORIZED, "Invalid API Key")

    enrolled = cached.get("hmac_enrolled", False)
    has_sig = bool(request.headers.get("X-Signature"))
    if enrolled and not has_sig:
        raise ApiError(ErrorCode.UNAUTHORIZED, "hmac signing required for this key")
    if not enrolled and has_sig:
        raise ApiError(ErrorCode.UNAUTHORIZED, "key not enrolled for hmac")

    # I3: timestamp ±window 前置 —— 廉价校验先做，避免海量伪造签名打爆 secret fetch 的 Redis/auth RTT。
    ts_raw = request.headers.get("X-Timestamp", "")
    try:
        ts = int(ts_raw)
    except ValueError:
        raise ApiError(ErrorCode.UNAUTHORIZED, "invalid timestamp") from None
    if abs(int(time.time()) - ts) > settings.hmac_timestamp_window_seconds:
        raise ApiError(ErrorCode.UNAUTHORIZED, "stale timestamp")

    # 取 secret：secret_blob 已由 pipeline 随 identity 一次取回（warm）；None → 冷回源。
    if secret_blob is not None:
        try:
            secret = crypto_mod.decrypt_secret(secret_blob)
        except Exception:  # InvalidTag / binascii / RuntimeError(缺 key) —— 非客户端错，503 + DEL 缓存
            await raw_client().delete(identity.hmac_secret_cache_key(api_key))
            raise ApiError(ErrorCode.INTERNAL, "hmac secret cache corrupt", http_status=503) from None
    else:
        key_id = cached.get("key_id")
        if not key_id:
            raise ApiError(
                ErrorCode.INTERNAL, "hmac secret fetch: missing key_id", http_status=503
            )
        client = _get_auth_httpx_client()
        try:
            resp = await client.post(
                settings.hmac_secret_service_url,
                json={"key_id": key_id},
                headers={"X-Internal-Service": settings.app_name},
            )
        except httpx.RequestError as e:
            raise ApiError(
                ErrorCode.INTERNAL,
                f"auth unreachable: {type(e).__name__}: {e!r}",
                http_status=503,
            ) from e
        if resp.status_code != 200:
            raise ApiError(ErrorCode.UNAUTHORIZED, "hmac secret fetch failed")
        secret = resp.json().get("hmac_secret")
        if not secret:
            raise ApiError(ErrorCode.UNAUTHORIZED, "key not enrolled for hmac")
        # I2: 回填 warm 缓存。encrypt 缺 HMAC_SECRET_KEY 抛 RuntimeError → 转 503（非裸 500）
        try:
            enc = crypto_mod.encrypt_secret(secret)
        except Exception:
            raise ApiError(
                ErrorCode.INTERNAL, "hmac encryption key not configured", http_status=503
            ) from None
        await identity.write_hmac_secret(
            api_key, enc, ttl=settings.hmac_nonce_ttl_seconds
        )

    # nonce SETNX（防重放：同一 nonce TTL 内只能用一次）。
    # C3: nonce_key 用 key_id，不把明文 api_key 写进 Redis（identity/secret 缓存都 hash，nonce 对齐）。
    nonce = request.headers.get("X-Nonce", "")
    if not nonce:
        raise ApiError(ErrorCode.UNAUTHORIZED, "invalid nonce")
    nonce_subject = cached.get("key_id") or hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    nonce_key = f"t:{cached['tenant_id']}:hmac:nonce:{nonce_subject}:{nonce}"
    set_ok = await raw_client().set(
        nonce_key, "1", ex=settings.hmac_nonce_ttl_seconds, nx=True
    )
    if not set_ok:
        raise ApiError(ErrorCode.UNAUTHORIZED, "replay detected")

    # verify（常时比对 hmac.compare_digest）
    body = await request.body()
    raw_path = request.url.path + (("?" + request.url.query) if request.url.query else "")
    if not signing.verify(
        secret, request.method, raw_path, body, ts_raw, request.headers.get("X-Signature", "")
    ):
        raise ApiError(ErrorCode.UNAUTHORIZED, "invalid signature")

    ctx = TenantContext(
        tenant_id=cached["tenant_id"],
        tenant_type=cached.get("tenant_type", "internal"),
        app_id=cached.get("app_id"),
        is_platform_admin=cached.get("is_platform_admin", False),
    )
    set_tenant_context(ctx)
    return ctx
