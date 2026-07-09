"""共享 fixtures（dispatcher tests）。"""

import os

# Settings 必填项（pg_host/pg_user/pg_password/redis_host）需在 import apihub_core /
# dispatcher.main（其 create_app 在 import 期即 get_settings()）之前注入。
# 仿 services/services/admin/tests/conftest.py 的模式。
_ENV_DEFAULTS = {
    "PG_HOST": "localhost",
    "PG_USER": "apihub",
    "PG_PASSWORD": "test",
    "REDIS_HOST": "localhost",
    "REDIS_PORT": "6379",
    "ENV": "test",
    "OTEL_SERVICE_NAME": "dispatcher-test",
}
for _k, _v in _ENV_DEFAULTS.items():
    os.environ.setdefault(_k, _v)

import pytest  # noqa: E402
from apihub_core.config import get_settings  # noqa: E402
from apihub_core.tenant import clear_tenant_context  # noqa: E402


@pytest.fixture(autouse=True)
def reset_tenant_context():
    clear_tenant_context()
    get_settings.cache_clear()
    yield
    clear_tenant_context()
    get_settings.cache_clear()


@pytest.fixture
def tenant_a():
    from apihub_core.tenant import TenantContext

    return TenantContext(
        tenant_id="tenant_a",
        tenant_type="internal",
        app_id="app_trading",
    )


@pytest.fixture
def async_client(monkeypatch, tenant_a):
    """带 tenant_a 上下文的 httpx.AsyncClient（ASGITransport）。

    鉴权被 monkeypatch 成固定 tenant_a，绕过 auth-svc/DB。
    生命周期不触发（plain ASGITransport 不跑 lifespan startup），
    故 app.state.workflow_client 不会由 main.py 自动注入——
    测试在发请求前直接覆写 async_client.app.state.workflow_client。
    """
    from apihub_core import auth as core_auth
    from dispatcher.main import app
    from httpx import ASGITransport, AsyncClient

    async def _no_auth(request, settings, api_key, required_scopes=None):
        from apihub_core.tenant import set_tenant_context

        set_tenant_context(tenant_a)
        return tenant_a

    monkeypatch.setattr(core_auth, "authenticate_request", _no_auth)

    client = AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    )
    # 暴露 app 引用：测试在发请求前直接写 app.state.workflow_client（fake），
    # 路由 handler 读 request.app.state.workflow_client 时拿到该 fake。
    client.app = app  # type: ignore[attr-defined]
    return client
