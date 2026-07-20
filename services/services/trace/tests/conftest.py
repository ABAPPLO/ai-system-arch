"""共享 fixtures（trace tests）。"""

import os

_ENV_DEFAULTS = {
    "PG_HOST": "localhost",
    "PG_USER": "apihub",
    "PG_PASSWORD": "test",
    "REDIS_HOST": "localhost",
    "REDIS_PORT": "6379",
    "CH_HOST": "localhost",
    "ENV": "test",
    "OTEL_SERVICE_NAME": "trace-test",
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


@pytest.fixture
def fake_ch(monkeypatch):
    """替换 ch.query_all / query_one，记录所有调用。"""
    from apihub_core import clickhouse as ch_mod

    state = {
        "rows": [],  # query_all 返回
        "row": None,  # query_one 返回
        "calls": [],  # [(sql, params, force_tenant_id)]
    }

    def _query_all(sql, params=None, *, force_tenant_id="sentinel"):
        state["calls"].append(("all", sql, params, force_tenant_id))
        return state["rows"]

    def _query_one(sql, params=None, *, force_tenant_id="sentinel"):
        state["calls"].append(("one", sql, params, force_tenant_id))
        return state["row"]

    def _query_union_peer(local_sql, peer_sql=None, params=None, *, force_tenant_id="sentinel"):
        # admin 跨区查询：记录为 union 调用，返回与 query_all 同样的 fixture 数据。
        state["calls"].append(("union", local_sql, params, force_tenant_id))
        return state["rows"]

    monkeypatch.setattr(ch_mod, "query_all", _query_all)
    monkeypatch.setattr(ch_mod, "query_one", _query_one)
    monkeypatch.setattr(ch_mod, "query_union_peer", _query_union_peer)
    return state


@pytest_asyncio.fixture
async def client(monkeypatch):
    """带默认 X-API-Key + 超管上下文的 test client。"""
    from trace_svc.main import build_app

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


@pytest.fixture
def as_normal_user(monkeypatch):
    """普通用户上下文。"""

    def _make(tenant_id: str = "t_demo"):
        ctx = TenantContext(
            tenant_id=tenant_id,
            tenant_type="internal",
            user_id="u_bob",
            is_platform_admin=False,
        )

        async def _fake(request, settings, api_key, required_scopes=None):
            set_tenant_context(ctx)
            return ctx

        from apihub_core import auth as core_auth

        monkeypatch.setattr(core_auth, "authenticate_request", _fake)
        return ctx

    return _make
