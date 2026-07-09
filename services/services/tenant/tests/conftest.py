"""共享 fixtures（tenant tests）。

mock 策略（同 quota 服务）：
  - DB 层：每个 test 自己 monkeypatch repository 的具体函数（避免实现完整 PG mock）
  - Redis：fakeredis（真 SETEX / DEL 行为）
  - 鉴权：用真实的 set_tenant_context / clear_tenant_context
"""

import os

_ENV_DEFAULTS = {
    "PG_HOST": "localhost",
    "PG_USER": "apihub",
    "PG_PASSWORD": "test",
    "REDIS_HOST": "localhost",
    "REDIS_PORT": "6379",
    "ENV": "test",
    "OTEL_SERVICE_NAME": "tenant-test",
}
for k, v in _ENV_DEFAULTS.items():
    os.environ.setdefault(k, v)

import pytest  # noqa: E402
from apihub_core.config import get_settings  # noqa: E402
from apihub_core.tenant import (  # noqa: E402
    TenantContext,
    clear_tenant_context,
    set_tenant_context,
)


@pytest.fixture(autouse=True)
def reset_state():
    clear_tenant_context()
    get_settings.cache_clear()
    yield
    clear_tenant_context()
    get_settings.cache_clear()


@pytest.fixture
def fake_redis(monkeypatch):
    import fakeredis.aioredis
    from apihub_core import redis as redis_mod

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_mod, "_client", fake)
    return fake


@pytest.fixture
def client(monkeypatch):
    """默认带 X-API-Key 头；鉴权逻辑由 authed fixture 接管。

    用法：
        async def test_x(client, as_platform_admin):
            ...
    """
    from httpx import ASGITransport, AsyncClient
    from tenant.main import app

    # 默认无上下文 —— 测试自己选 authed/as_platform_admin/as_normal_user
    async def _no_auth(request, settings, api_key, required_scopes=None):
        from apihub_core.tenant import TenantContext

        # 没设置上下文就给个默认（很多测试不关心身份，只要有上下文）
        ctx = TenantContext(tenant_id="t_default", tenant_type="internal", user_id="u_default")
        set_tenant_context(ctx)
        return ctx

    from apihub_core import auth as core_auth

    monkeypatch.setattr(core_auth, "authenticate_request", _no_auth)

    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-API-Key": "ak_test"},
    )


@pytest.fixture
def authed(monkeypatch):
    """让 middleware 透传 —— 提供 _set_ctx 回调在测试中切上下文。

    用法：
        ctx = authed(user_id="user_alice", tenant_id="t1", is_admin=False)
    """
    from apihub_core import auth as core_auth

    def _set_ctx(*, user_id, tenant_id="tenant_a", is_admin=False):
        ctx = TenantContext(
            tenant_id=tenant_id,
            tenant_type="internal",
            user_id=user_id,
            is_platform_admin=is_admin,
        )

        async def _fake_auth(request, settings, api_key, required_scopes=None):
            set_tenant_context(ctx)
            return ctx

        monkeypatch.setattr(core_auth, "authenticate_request", _fake_auth)
        return ctx

    return _set_ctx


@pytest.fixture
def as_platform_admin(authed):
    """超管上下文。"""
    return authed(user_id="user_admin", tenant_id="system", is_admin=True)


@pytest.fixture
def as_normal_user(authed):
    """普通用户上下文（user_id 由参数指定）。同步调用。"""

    def _make(user_id: str, tenant_id: str = "tenant_a"):
        return authed(user_id=user_id, tenant_id=tenant_id, is_admin=False)

    return _make
