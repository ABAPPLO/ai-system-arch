"""identity 缓存契约测试 —— 与 auth.cache 共享单一真相源。"""

import json

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("PG_HOST", "localhost")
    monkeypatch.setenv("PG_USER", "apihub")
    monkeypatch.setenv("PG_PASSWORD", "test")
    monkeypatch.setenv("REDIS_HOST", "localhost")
    from apihub_core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_identity_cache_key_is_ak_sha256():
    import hashlib

    from apihub_core.identity import identity_cache_key

    api_key = "ak_abc123"
    assert identity_cache_key(api_key) == "ak:" + hashlib.sha256(api_key.encode()).hexdigest()


async def test_write_then_read_identity(monkeypatch):
    stored = {}

    class _FakeRedis:
        async def setex(self, key, ttl, val):
            stored[key] = (ttl, val)

        async def get(self, key):
            return stored.get(key, (None, None))[1]

        async def delete(self, key):
            stored.pop(key, None)

    from apihub_core import identity
    from apihub_core import redis as redis_mod

    monkeypatch.setattr(redis_mod, "raw_client", lambda: _FakeRedis())

    await identity.write_identity("ak_x", {"tenant_id": "t1", "app_id": "a1"}, ttl=300)
    got = await identity.read_identity("ak_x")
    assert got == {"tenant_id": "t1", "app_id": "a1"}


async def test_read_identity_miss_returns_none(monkeypatch):
    class _FakeRedis:
        async def get(self, key):
            return None

    from apihub_core import identity
    from apihub_core import redis as redis_mod

    monkeypatch.setattr(redis_mod, "raw_client", lambda: _FakeRedis())
    assert await identity.read_identity("ak_x") is None


async def test_delete_identity(monkeypatch):
    class _FakeRedis:
        async def delete(self, key):
            pass

    from apihub_core import identity
    from apihub_core import redis as redis_mod

    monkeypatch.setattr(redis_mod, "raw_client", lambda: _FakeRedis())
    await identity.delete_identity("ak_x")  # 不抛即通过


async def test_authenticate_request_trust_path_skips_http(monkeypatch):
    """X-Ingress-Auth 匹配 + Redis 命中 → 跳过 HTTP auth，直接建 ctx。"""
    from apihub_core.config import get_settings

    monkeypatch.setenv("INGRESS_SHARED_SECRET", "s3cr3t")
    get_settings.cache_clear()

    cached = {
        "is_active": True,
        "tenant_id": "t1",
        "tenant_type": "internal",
        "app_id": "app1",
        "is_platform_admin": False,
    }

    class _FakeRedis:
        async def get(self, key):
            return json.dumps(cached)

    from apihub_core import redis as redis_mod
    from apihub_core.auth import authenticate_request

    monkeypatch.setattr(redis_mod, "raw_client", lambda: _FakeRedis())

    class _FakeRequest:
        def __init__(self):
            self.headers = {"X-API-Key": "ak_real", "X-Ingress-Auth": "s3cr3t"}

    http_called = []

    class _NoHttp:
        def __init__(self, *a, **kw):
            http_called.append(True)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *a, **kw):
            raise AssertionError("trust path must not call auth HTTP")

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _NoHttp)

    ctx = await authenticate_request(_FakeRequest(), get_settings(), "ak_real")
    assert ctx.tenant_id == "t1"
    assert ctx.app_id == "app1"
    assert http_called == []  # 没建 http client
    get_settings.cache_clear()


async def test_authenticate_request_trust_path_miss_falls_back_to_http(monkeypatch):
    """X-Ingress-Auth 匹配但 Redis miss → 回落 HTTP auth（预热缓存）。"""
    from apihub_core.config import get_settings

    monkeypatch.setenv("INGRESS_SHARED_SECRET", "s3cr3t")
    get_settings.cache_clear()

    class _FakeRedis:
        async def get(self, key):
            return None  # miss

    from apihub_core import redis as redis_mod
    from apihub_core.auth import authenticate_request

    monkeypatch.setattr(redis_mod, "raw_client", lambda: _FakeRedis())

    class _FakeResp:
        status_code = 200

        def json(self):
            return {
                "is_active": True,
                "tenant_id": "t2",
                "tenant_type": "internal",
                "app_id": "app2",
                "is_platform_admin": False,
            }

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *a, **kw):
            return _FakeResp()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    class _FakeRequest:
        def __init__(self):
            self.headers = {"X-API-Key": "ak_real", "X-Ingress-Auth": "s3cr3t"}

    ctx = await authenticate_request(_FakeRequest(), get_settings(), "ak_real")
    assert ctx.tenant_id == "t2"  # 来自 HTTP 回落
    get_settings.cache_clear()
