"""HTTP 端点测试 —— 用 httpx ASGITransport 直接打 app，mock 掉 DB + Redis + 鉴权。

verify 是核心入口，覆盖：格式校验 / 缓存命中（正、负）/ DB 命中 / 缓存写入。
create/list/revoke 走 RLS 同租户路径，需要把 authenticate_request 替换成 spy。
"""

from datetime import UTC, datetime

import pytest
from apihub_core import auth as core_auth
from apihub_core import redis as redis_mod
from apihub_core.tenant import TenantContext, set_tenant_context
from auth import routes as routes_mod
from auth.main import app
from httpx import ASGITransport, AsyncClient


class _FakeRedis:
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
    fake = _FakeRedis()
    monkeypatch.setattr(redis_mod, "_client", fake)
    return fake


@pytest.fixture
def authed(monkeypatch):
    """让 middleware 把 tenant 设成 t1/app_x —— 用于受保护的端点。"""

    async def _fake_authenticate(request, settings, api_key, required_scopes=None):
        ctx = TenantContext(
            tenant_id="t1",
            tenant_type="internal",
            app_id="app_x",
            is_platform_admin=False,
        )
        set_tenant_context(ctx)
        return ctx

    monkeypatch.setattr(core_auth, "authenticate_request", _fake_authenticate)


@pytest.fixture
def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ========== /v1/apikey/verify (内部端点，跳过 auth middleware) ==========


class TestVerify:
    async def test_rejects_garbage_format(self, client, fake_redis):
        """格式错误直接拒绝（pydantic 通过但 is_valid_format 不过），不查 DB 不查缓存。"""
        resp = await client.post(
            "/v1/apikey/verify",
            json={"api_key": "garbage_no_ak_prefix_long_enough"},
        )
        assert resp.status_code == 401
        assert fake_redis.store == {}

    async def test_rejects_missing_field(self, client):
        resp = await client.post("/v1/apikey/verify", json={})
        assert resp.status_code == 422  # pydantic 校验失败

    async def test_cache_positive_hit_skips_db(self, client, fake_redis, monkeypatch):
        """缓存里有正结果 → 直接返回，不打 DB。"""
        plaintext = "ak_" + "a" * 40
        from auth.apikey import cache_key

        fake_redis.store[cache_key(plaintext)] = (
            '{"is_active": true, "tenant_id": "t1", "tenant_type": "internal", '
            '"app_id": "a1", "is_platform_admin": false, "scopes": []}'
        )

        # DB 不应被调用
        async def _should_not_call(p):
            raise AssertionError("DB should not be hit on cache positive")

        from auth import repository as r

        monkeypatch.setattr(r, "verify_api_key_record", _should_not_call)
        routes_mod.verify_api_key_record = r.verify_api_key_record

        resp = await client.post("/v1/apikey/verify", json={"api_key": plaintext})

        assert resp.status_code == 200
        body = resp.json()
        assert body["tenant_id"] == "t1"
        assert body["app_id"] == "a1"

    async def test_cache_negative_hit_returns_401(self, client, fake_redis, monkeypatch):
        """负缓存命中 → 401，不打 DB。"""
        plaintext = "ak_" + "b" * 40
        from auth.apikey import cache_key

        fake_redis.store[cache_key(plaintext)] = '{"invalid": true}'

        async def _should_not_call(p):
            raise AssertionError("DB should not be hit on cache negative")

        from auth import repository as r

        monkeypatch.setattr(r, "verify_api_key_record", _should_not_call)
        routes_mod.verify_api_key_record = r.verify_api_key_record

        resp = await client.post("/v1/apikey/verify", json={"api_key": plaintext})
        assert resp.status_code == 401

    async def test_db_hit_writes_positive_cache(self, client, fake_redis, monkeypatch):
        """DB 找到 → 写正缓存，返回 200。"""
        plaintext = "ak_" + "c" * 40

        async def _v(p):
            return {
                "is_active": True,
                "tenant_id": "t_db",
                "tenant_type": "internal",
                "app_id": "app_db",
                "is_platform_admin": False,
                "scopes": ["read"],
            }

        from auth import repository as r

        monkeypatch.setattr(r, "verify_api_key_record", _v)
        routes_mod.verify_api_key_record = r.verify_api_key_record

        resp = await client.post("/v1/apikey/verify", json={"api_key": plaintext})

        assert resp.status_code == 200
        body = resp.json()
        assert body["tenant_id"] == "t_db"
        assert body["scopes"] == ["read"]

        from auth.apikey import cache_key

        assert cache_key(plaintext) in fake_redis.store  # 写了正缓存

    async def test_db_miss_writes_negative_cache(self, client, fake_redis, monkeypatch):
        """DB 找不到 → 写负缓存，返回 401。"""
        plaintext = "ak_" + "d" * 40

        async def _v(p):
            return None

        from auth import repository as r

        monkeypatch.setattr(r, "verify_api_key_record", _v)
        routes_mod.verify_api_key_record = r.verify_api_key_record

        resp = await client.post("/v1/apikey/verify", json={"api_key": plaintext})

        assert resp.status_code == 401
        from auth.apikey import cache_key

        assert cache_key(plaintext) in fake_redis.store  # 写了负缓存


# ========== /v1/apps/{app_id}/api-keys (受保护端点) ==========


class TestCreateKey:
    async def test_requires_api_key(self, client):
        """没 API Key header → 401。"""
        resp = await client.post(
            "/v1/apps/app_x/api-keys",
            json={"name": "my key"},
        )
        assert resp.status_code == 401

    async def test_creates_and_returns_plaintext_once(self, client, authed, monkeypatch):
        """鉴权通过 → 创建成功，明文 api_key 仅此一次返回。"""
        captured = {}

        async def _create(**kwargs):
            captured.update(kwargs)
            return {
                "id": kwargs["key_id"],
                "app_id": kwargs["app_id"],
                "name": kwargs["name"],
                "scopes": kwargs["scopes"],
                "display_prefix": kwargs["display_prefix"],
                "expires_at": kwargs["expires_at"],
                "created_at": datetime.now(UTC).isoformat(),
            }

        from auth import repository as r

        monkeypatch.setattr(r, "create_api_key", _create)
        routes_mod.create_api_key = r.create_api_key

        resp = await client.post(
            "/v1/apps/app_x/api-keys",
            json={"name": "prod key", "scopes": ["read", "write"]},
            headers={"X-API-Key": "ak_test"},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["api_key"].startswith("ak_")  # 明文
        assert body["name"] == "prod key"
        assert body["scopes"] == ["read", "write"]
        # 写到 DB 的参数对（tenant_id 来自 middleware 注入的 ctx）
        assert captured["tenant_id"] == "t1"
        assert captured["app_id"] == "app_x"
        assert captured["name"] == "prod key"


class TestListKeys:
    async def test_returns_list_without_plaintext(self, client, authed, monkeypatch):
        from auth import repository as r

        async def _l(app_id):
            return [
                {
                    "id": "key_1",
                    "app_id": app_id,
                    "name": "k1",
                    "scopes": [],
                    "display_prefix": "ak_abcde",
                    "status": "active",
                    "last_used_at": None,
                    "expires_at": None,
                    "created_at": "2026-01-01T00:00:00",
                    "revoked_at": None,
                }
            ]

        monkeypatch.setattr(r, "list_api_keys_for_app", _l)
        routes_mod.list_api_keys_for_app = r.list_api_keys_for_app

        resp = await client.get(
            "/v1/apps/app_x/api-keys",
            headers={"X-API-Key": "ak_test"},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["id"] == "key_1"
        assert "api_key" not in body[0]  # 列表项不含明文


# ========== DELETE /v1/api-keys/{key_id} ==========


class TestRevoke:
    async def test_revokes_and_invalidates_cache(self, client, fake_redis, authed, monkeypatch):
        """吊销 → DB UPDATE + 清缓存。"""
        from auth.apikey import cache_key

        cache_k = cache_key("somehashvalue")
        fake_redis.store[cache_k] = '{"is_active": true}'

        from auth import repository as r

        async def _rv(key_id):
            return {"id": key_id, "app_id": "app_x", "key_hash": "somehashvalue"}

        monkeypatch.setattr(r, "revoke_api_key", _rv)
        routes_mod.revoke_api_key = r.revoke_api_key

        resp = await client.delete(
            "/v1/api-keys/key_abc",
            headers={"X-API-Key": "ak_test"},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "revoked"
        assert cache_k not in fake_redis.store  # 缓存被清

    async def test_revoke_missing_returns_404(self, client, authed, monkeypatch):
        from apihub_core.errors import ApiError, ErrorCode
        from auth import repository as r

        async def _rv(key_id):
            raise ApiError(ErrorCode.NOT_FOUND, "not found")

        monkeypatch.setattr(r, "revoke_api_key", _rv)
        routes_mod.revoke_api_key = r.revoke_api_key

        resp = await client.delete(
            "/v1/api-keys/key_missing",
            headers={"X-API-Key": "ak_test"},
        )
        assert resp.status_code == 404


# ========== /v1/auth/health (skip-auth) ==========


class TestHealth:
    async def test_health_returns_ok(self, client):
        resp = await client.get("/v1/auth/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "service": "auth"}
