"""R2e T6: auth HMAC routes 单测（httpx ASGI 打真路由，monkeypatch repository）。

覆盖：
  - create-key {signing:true} → create_api_key 收 signing=True + 响应带 hmac_secret（明文仅一次）
  - create-key signing 缺省 → hmac_secret=None
  - rotate 端点 → 新明文返回一次 + 失效 hmac_secret:{key_hash} 缓存 + 不回传 key_hash
  - /v1/internal/hmac-secret（skip_auth）冷路径 → 取明文 / 未 enrolled 返 null
"""

import pytest
from apihub_core import auth as core_auth
from apihub_core.tenant import TenantContext, set_tenant_context
from auth import routes as routes_mod
from auth.main import app
from httpx import ASGITransport, AsyncClient


@pytest.fixture(autouse=True)
def _hmac_key(monkeypatch):
    """crypto.encrypt_secret 需要 HMAC_SECRET_KEY（conftest 默认不设）。"""
    monkeypatch.setenv("HMAC_SECRET_KEY", "a" * 64)
    from apihub_core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def authed(monkeypatch):
    """让 middleware 把 caller 设成 t1/app_x（受保护端点用）。"""

    async def _fake(request, settings, api_key, required_scopes=None):
        ctx = TenantContext(
            tenant_id="t1",
            tenant_type="internal",
            app_id="app_x",
            is_platform_admin=False,
        )
        set_tenant_context(ctx)
        return ctx

    monkeypatch.setattr(core_auth, "authenticate_request", _fake)


async def _noop(*a, **kw):
    """best-effort warmup 的 async 占位（consumer upsert / identity / secret 缓存写）。"""
    return None


# ========== POST /v1/apps/{app_id}/api-keys {signing: true} ==========


class TestCreateKeySigning:
    async def test_signing_true_returns_hmac_secret(self, client, authed, monkeypatch):
        """signing=True → create_api_key 收到 signing=True，响应带 hmac_secret（仅一次）。"""
        captured: dict = {}

        async def _create(**kwargs):
            captured.update(kwargs)
            return {
                "id": kwargs["key_id"],
                "app_id": kwargs["app_id"],
                "name": kwargs["name"],
                "scopes": kwargs["scopes"],
                "display_prefix": "ak_xxxxxxxx",
                "expires_at": kwargs["expires_at"],
                "created_at": "2026-07-21T00:00:00",
                "hmac_secret": "generated_secret_value",
            }

        monkeypatch.setattr(routes_mod, "create_api_key", _create)
        # 中和 best-effort warmup，避免 consumer upsert / 真缓存写
        monkeypatch.setattr(routes_mod, "_inject_home_region_on_create", _noop)
        import apihub_core.identity as ident

        monkeypatch.setattr(ident, "write_identity", _noop)
        monkeypatch.setattr(ident, "write_hmac_secret", _noop)

        resp = await client.post(
            "/v1/apps/app_x/api-keys",
            json={"name": "hmac key", "signing": True},
            headers={"X-API-Key": "ak_test"},
        )

        assert resp.status_code == 200, resp.text
        assert captured.get("signing") is True
        assert resp.json()["hmac_secret"] == "generated_secret_value"

    async def test_signing_false_omits_hmac_secret(self, client, authed, monkeypatch):
        """signing 缺省=False → create_api_key 收 signing=False，响应 hmac_secret=None。"""
        captured: dict = {}

        async def _create(**kwargs):
            captured.update(kwargs)
            return {
                "id": kwargs["key_id"],
                "app_id": kwargs["app_id"],
                "name": kwargs["name"],
                "scopes": kwargs["scopes"],
                "display_prefix": "ak_xxxxxxxx",
                "expires_at": kwargs["expires_at"],
                "created_at": "2026-07-21T00:00:00",
                "hmac_secret": None,
            }

        monkeypatch.setattr(routes_mod, "create_api_key", _create)
        monkeypatch.setattr(routes_mod, "_inject_home_region_on_create", _noop)
        import apihub_core.identity as ident

        monkeypatch.setattr(ident, "write_identity", _noop)
        monkeypatch.setattr(ident, "write_hmac_secret", _noop)

        resp = await client.post(
            "/v1/apps/app_x/api-keys",
            json={"name": "plain key"},
            headers={"X-API-Key": "ak_test"},
        )

        assert resp.status_code == 200, resp.text
        assert captured.get("signing") is False
        assert resp.json()["hmac_secret"] is None


# ========== POST /v1/api-keys/{key_id}/hmac-secret/rotate ==========


class TestRotate:
    async def test_rotate_returns_new_secret_and_invalidates_cache(
        self, client, authed, fake_redis, monkeypatch
    ):
        """rotate → 新明文返回一次 + 失效 hmac_secret:{key_hash} + 不回传 key_hash。"""
        async def _rotate(key_id, tenant_id):
            assert tenant_id == "t1"  # C1: route 传 caller tenant_id 进 repo 过滤
            return {"key_id": key_id, "key_hash": "abc", "hmac_secret": "new_secret"}

        monkeypatch.setattr(routes_mod, "rotate_hmac_secret", _rotate)

        # 预置一条 warm secret 缓存，验 rotate 把它清掉
        await fake_redis.set("hmac_secret:abc", "stale_blob")

        resp = await client.post(
            "/v1/api-keys/key_1/hmac-secret/rotate",
            headers={"X-API-Key": "ak_test"},
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["key_id"] == "key_1"
        assert body["hmac_secret"] == "new_secret"
        assert "key_hash" not in body  # 内部用，不回传
        assert await fake_redis.get("hmac_secret:abc") is None  # 缓存已失效


# ========== POST /v1/internal/hmac-secret（skip_auth，dispatcher 冷路径）==========


class TestInternalHmacSecret:
    async def test_cold_path_returns_secret(self, client, monkeypatch):
        async def _get(key_id):
            return "cold_secret"

        monkeypatch.setattr(routes_mod, "get_hmac_secret_plaintext", _get)

        resp = await client.post("/v1/internal/hmac-secret", json={"key_id": "key_abcdef"})

        assert resp.status_code == 200, resp.text
        assert resp.json()["hmac_secret"] == "cold_secret"

    async def test_unenrolled_returns_null(self, client, monkeypatch):
        """未 enrolled（列 NULL）→ hmac_secret=None（非 401）。"""
        async def _get(key_id):
            return None

        monkeypatch.setattr(routes_mod, "get_hmac_secret_plaintext", _get)

        resp = await client.post("/v1/internal/hmac-secret", json={"key_id": "key_abcdef"})

        assert resp.status_code == 200, resp.text
        assert resp.json()["hmac_secret"] is None
