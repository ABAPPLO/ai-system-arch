"""共享 fixtures（portal tests）。

镜像 admin/tests/conftest.py，但 client fixture 带 `Authorization: Bearer <jwt>`
头（portal 是人认证场景），且默认上下文是 external-public 租户。
"""

import os

_ENV_DEFAULTS = {
    "PG_HOST": "localhost",
    "PG_USER": "apihub",
    "PG_PASSWORD": "test",
    "REDIS_HOST": "localhost",
    "REDIS_PORT": "6379",
    "ENV": "test",
    "OTEL_SERVICE_NAME": "portal-test",
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
    """带 `Authorization: Bearer <jwt>` 头的 test client —— 模拟 Portal 前端。

    JWT 分流（Task 1）后，middleware 设 TenantContext(external-public)。
    """
    from apihub_core import auth as core_auth
    from httpx import ASGITransport, AsyncClient
    from portal.main import app

    async def _jwt_auth(request, settings, api_key, required_scopes=None):
        ctx = TenantContext(
            tenant_id="external-public",
            tenant_type="external",
            user_id="u_test",
        )
        set_tenant_context(ctx)
        return ctx

    monkeypatch.setattr(core_auth, "authenticate_request", _jwt_auth)

    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer eyJ.test.token"},
    )
