"""authenticate_request JWT 分支集成行为测试。

jwt_utils 已有纯函数单测（test_jwt_utils.py）；本文件覆盖
auth.py:35-47 的 JWT 分流集成行为：
  - 有效 JWT  → TenantContext(tenant_type="external") 并注入 contextvar
  - 错 secret → ApiError(UNAUTHORIZED)
  - 已过期    → ApiError(UNAUTHORIZED)
JWT 分支本地验签（不调 httpx），故无需 mock auth 服务。
"""

import pytest

from apihub_core import jwt_utils
from apihub_core.auth import authenticate_request
from apihub_core.config import Settings
from apihub_core.errors import ApiError, ErrorCode
from apihub_core.tenant import get_tenant_context

# Settings 必填字段（无默认）—— 参 test_config.py 同模式。
_REQUIRED = {
    "pg_host": "localhost",
    "pg_user": "apihub",
    "pg_password": "test",  # noqa: S106
    "redis_host": "localhost",
}

# JWT 分支不引用 request 参数（仅 API Key httpx 分支理论可能用），传 None 即可。
_UNUSED_REQUEST = None  # type: ignore[assignment]


def _make_settings(jwt_secret: str) -> Settings:
    """构造带指定 jwt_secret 的 Settings（kwarg 覆盖优先于 env/默认）。"""
    return Settings(**_REQUIRED, jwt_secret=jwt_secret)


async def test_jwt_branch_valid_token():
    """有效 JWT → TenantContext(tenant_type='external') + 注入 contextvar。"""
    secret = "test-secret-A"
    settings = _make_settings(secret)
    token = jwt_utils.issue_token(
        user_id="u_dev_1",
        tenant_id="external-public",
        secret=secret,
        ttl_seconds=60,
    )

    ctx = await authenticate_request(_UNUSED_REQUEST, settings, token)

    assert ctx.tenant_type == "external"
    assert ctx.tenant_id == "external-public"
    assert ctx.user_id == "u_dev_1"
    assert ctx.is_platform_admin is False
    # 副作用：auth.py 内部 set_tenant_context(ctx)
    assert get_tenant_context() is ctx


async def test_jwt_branch_wrong_secret():
    """secret 'A' 签 token，settings.jwt_secret='B' → UNAUTHORIZED 401。"""
    token = jwt_utils.issue_token(
        user_id="u", tenant_id="t", secret="secret-A", ttl_seconds=60
    )
    settings = _make_settings(jwt_secret="secret-B")

    with pytest.raises(ApiError) as exc_info:
        await authenticate_request(_UNUSED_REQUEST, settings, token)

    assert exc_info.value.code == ErrorCode.UNAUTHORIZED
    assert exc_info.value.http_status == 401


async def test_jwt_branch_expired():
    """ttl_seconds=-1（exp 在过去）→ 本地验签失败 → UNAUTHORIZED 401。"""
    secret = "test-secret-A"
    settings = _make_settings(secret)
    token = jwt_utils.issue_token(
        user_id="u", tenant_id="t", secret=secret, ttl_seconds=-1
    )

    with pytest.raises(ApiError) as exc_info:
        await authenticate_request(_UNUSED_REQUEST, settings, token)

    assert exc_info.value.code == ErrorCode.UNAUTHORIZED
    assert exc_info.value.http_status == 401


def test_is_jwt_guard_does_not_misfire_on_apikey():
    """纯函数断言：API Key（'ak_' 前缀）不被误判为 JWT，不进 JWT 分流。"""
    assert jwt_utils.is_jwt("ak_abcdef123456") is False
