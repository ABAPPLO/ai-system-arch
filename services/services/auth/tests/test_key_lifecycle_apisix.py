"""create_key/revoke_key 的 APISIX consumer + Redis 缓存副作用测试。"""

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    # auth tests/conftest.py 已注入最小 env；补 APISIX
    monkeypatch.setenv("APISIX_ADMIN_URL", "http://apisix-admin.apihub-ingress:9180")
    monkeypatch.setenv("APISIX_ADMIN_KEY", "edd1c9f034335f136f87ad84b625c8f1")
    from apihub_core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class _FakeRedis:
    """最小 fake —— 用 dict 模拟 raw_client（与 test_routes/test_cache 一致）。"""

    def __init__(self):
        self.store: dict[str, str] = {}

    async def setex(self, key, ttl, value):
        self.store[key] = value
        return True

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        return self.store.pop(key, None)


@pytest.fixture
def fake_redis(monkeypatch):
    from apihub_core import redis as redis_mod

    fake = _FakeRedis()
    monkeypatch.setattr(redis_mod, "_client", fake)
    return fake


async def test_create_key_upserts_consumer_and_warms_cache(monkeypatch):
    calls = {"consumer": None, "cache_warm": None}

    async def _upsert(*, key_id, key, labels=None):
        calls["consumer"] = (key_id, key, labels)

    async def _write(api_key, data, ttl):
        calls["cache_warm"] = (api_key, data, ttl)

    from apihub_core import identity
    from auth import routes as routes_mod

    # R3b S1-T3：create_key 现在通过 _inject_home_region_on_create 调用
    # routes_mod.upsert_consumer（模块级绑定），并先查 get_tenant_home_region。
    async def _home_region(tenant_id):  # noqa: ARG001
        return "bj"

    monkeypatch.setattr(routes_mod, "upsert_consumer", _upsert)
    monkeypatch.setattr(routes_mod, "get_tenant_home_region", _home_region)
    monkeypatch.setattr(identity, "write_identity", _write)

    async def _fake_create(**kw):
        return {
            "id": kw["key_id"],
            "app_id": kw["app_id"],
            "name": kw["name"],
            "scopes": kw["scopes"],
            "display_prefix": kw["display_prefix"],
            "expires_at": kw["expires_at"],
            "created_at": "2026-07-17T00:00:00+00:00",
        }

    monkeypatch.setattr(routes_mod, "create_api_key", _fake_create)

    from apihub_core import auth as auth_mod
    from apihub_core import kafka as k_mod
    from apihub_core.tenant import TenantContext, set_tenant_context

    async def _noop_emit(*a, **kw):
        pass

    monkeypatch.setattr(k_mod, "emit", _noop_emit)

    ctx = TenantContext(tenant_id="t1", tenant_type="internal", is_platform_admin=True)

    async def _noop_auth(request, settings, api_key):  # noqa: ARG001
        set_tenant_context(ctx)
        return ctx

    monkeypatch.setattr(auth_mod, "authenticate_request", _noop_auth)

    import httpx
    from auth.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        resp = await c.post(
            "/v1/apps/app_1/api-keys",
            headers={"X-API-Key": "ak_x"},
            json={"name": "nn", "scopes": []},
        )
    assert resp.status_code == 200, resp.text
    plaintext = resp.json()["api_key"]
    assert calls["consumer"][0] == resp.json()["id"]  # key_id
    assert calls["consumer"][1] == plaintext
    assert calls["cache_warm"][0] == plaintext
    assert calls["cache_warm"][1]["app_id"] == "app_1"


async def test_revoke_key_deletes_consumer_and_cache(monkeypatch, fake_redis):
    from auth.apikey import cache_key

    # 预填缓存（key_hash="h"），验证 revoke 后被 invalidate 清掉
    cache_k = cache_key("h")
    fake_redis.store[cache_k] = '{"is_active": true}'

    calls = {"consumer_del": None}

    async def _delete_consumer(key_id):
        calls["consumer_del"] = key_id

    from apihub_core import apisix_client
    from auth import routes as routes_mod

    monkeypatch.setattr(apisix_client, "delete_consumer", _delete_consumer)

    async def _fake_revoke(key_id):
        return {"id": key_id, "app_id": "app_1", "key_hash": "h"}

    monkeypatch.setattr(routes_mod, "revoke_api_key", _fake_revoke)

    from apihub_core import auth as auth_mod
    from apihub_core import kafka as k_mod
    from apihub_core.tenant import TenantContext, set_tenant_context

    async def _noop_emit(*a, **kw):
        pass

    monkeypatch.setattr(k_mod, "emit", _noop_emit)

    ctx = TenantContext(tenant_id="t1", tenant_type="internal", is_platform_admin=True)

    async def _noop_auth(request, settings, api_key):  # noqa: ARG001
        set_tenant_context(ctx)
        return ctx

    monkeypatch.setattr(auth_mod, "authenticate_request", _noop_auth)

    import httpx
    from auth.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        resp = await c.delete("/v1/api-keys/key_1", headers={"X-API-Key": "ak_x"})
    assert resp.status_code == 200, resp.text
    assert calls["consumer_del"] == "key_1"
    # invalidate 入参是 key_hash（非明文）—— identity.delete_identity 用明文算 key 会失配，
    # 故 cache.invalidate 走 auth.apikey.cache_key + raw_client.delete（兼容明文/-hash）。
    # 此处验证缓存确实被清。
    assert cache_k not in fake_redis.store
