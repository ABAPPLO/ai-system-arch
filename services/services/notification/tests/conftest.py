"""共享 fixtures（notification tests）。"""

import os

_ENV_DEFAULTS = {
    "PG_HOST": "localhost",
    "PG_USER": "apihub",
    "PG_PASSWORD": "test",
    "REDIS_HOST": "localhost",
    "REDIS_PORT": "6379",
    "ENV": "test",
    "OTEL_SERVICE_NAME": "notification-test",
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
def client(monkeypatch):
    """带默认 X-API-Key 头的 test client。"""
    from httpx import ASGITransport, AsyncClient
    from notification.main import build_app

    app = build_app()

    async def _no_auth(request, settings, api_key, required_scopes=None):
        ctx = TenantContext(
            tenant_id="t_default",
            tenant_type="internal",
            user_id="u_default",
        )
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
def as_platform_admin(monkeypatch):
    """超管上下文。"""
    ctx = TenantContext(
        tenant_id="system",
        tenant_type="system",
        user_id="u_admin",
        is_platform_admin=True,
    )

    async def _fake(request, settings, api_key, required_scopes=None):
        set_tenant_context(ctx)
        return ctx

    from apihub_core import auth as core_auth

    monkeypatch.setattr(core_auth, "authenticate_request", _fake)
    return ctx


@pytest.fixture
def any_tenant(monkeypatch):
    """创建指定上下文（工厂 fixture）。"""

    def _make(tenant_id: str = "tenant_a", user_id: str = "u_bob"):
        ctx = TenantContext(
            tenant_id=tenant_id,
            tenant_type="internal",
            user_id=user_id,
        )

        async def _fake(request, settings, api_key, required_scopes=None):
            set_tenant_context(ctx)
            return ctx

        from apihub_core import auth as core_auth

        monkeypatch.setattr(core_auth, "authenticate_request", _fake)
        return ctx

    return _make
