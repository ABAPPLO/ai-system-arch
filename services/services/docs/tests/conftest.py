"""共享 fixtures（docs tests）。"""

import os

# 必须在 import apihub_core.config 之前设置
_ENV_DEFAULTS = {
    "PG_HOST": "localhost",
    "PG_USER": "apihub",
    "PG_PASSWORD": "test",
    "REDIS_HOST": "localhost",
    "REDIS_PORT": "6379",
    "ENV": "test",
    "OTEL_SERVICE_NAME": "docs-test",
}
for k, v in _ENV_DEFAULTS.items():
    os.environ.setdefault(k, v)

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from apihub_core.config import get_settings  # noqa: E402
from apihub_core.tenant import (  # noqa: E402
    TenantContext,
    clear_tenant_context,
    set_tenant_context,
)
from httpx import ASGITransport, AsyncClient  # noqa: E402


@pytest.fixture(autouse=True)
def reset_state():
    clear_tenant_context()
    get_settings.cache_clear()
    yield
    clear_tenant_context()
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def client(monkeypatch):
    """带默认 X-API-Key 头的 test client。

    跳过真实鉴权 —— 直接给一个 TenantContext。
    """
    from docs.main import build_app

    app = build_app()

    ctx = TenantContext(
        tenant_id="t_demo",
        tenant_type="internal",
        user_id="u_admin",
        is_platform_admin=True,
    )

    async def _no_auth(request, settings, api_key, required_scopes=None):
        set_tenant_context(ctx)
        return ctx

    from apihub_core import auth as core_auth

    monkeypatch.setattr(core_auth, "authenticate_request", _no_auth)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-API-Key": "ak_test"},
    ) as c:
        yield c
