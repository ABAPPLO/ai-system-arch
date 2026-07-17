"""apisix_client 单测 —— stub httpx，断言 Admin API 请求形状。"""

import httpx
import pytest
from apihub_core.errors import ApiError


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    from apihub_core.config import get_settings

    # core conftest 不注入必填 env，这里自给（Settings 构造需要 PG/REDIS）
    monkeypatch.setenv("PG_HOST", "localhost")
    monkeypatch.setenv("PG_USER", "apihub")
    monkeypatch.setenv("PG_PASSWORD", "test")
    monkeypatch.setenv("REDIS_HOST", "localhost")
    monkeypatch.setenv("APISIX_ADMIN_URL", "http://apisix-admin.apihub-ingress:9180")
    monkeypatch.setenv("APISIX_ADMIN_KEY", "edd1c9f034335f136f87ad84b625c8f1")
    monkeypatch.setenv("DISPATCHER_UPSTREAM", "dispatcher.apihub-system:8001")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_publish_route_puts_admin_route(monkeypatch):
    captured = {}

    class _FakeResp:
        status_code = 201

        def json(self):
            return {"ok": True}

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def request(self, method, url, **kw):
            captured["method"] = method
            captured["url"] = url
            captured["headers"] = kw.get("headers")
            captured["json"] = kw.get("json")
            return _FakeResp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    from apihub_core import apisix_client

    await apisix_client.publish_route(
        version_id="ver_abc",
        method="GET",
        path="/users/{user_id}",
        base_path="/v1",
    )

    assert captured["method"] == "PUT"
    assert (
        captured["url"]
        == "http://apisix-admin.apihub-ingress:9180/apisix/admin/routes/ver_abc"
    )
    assert captured["headers"]["X-API-KEY"] == "edd1c9f034335f136f87ad84b625c8f1"
    body = captured["json"]
    assert body["uri"] == "/v1/users/:user_id"  # base_path + path，{var}→:var
    assert body["methods"] == ["GET"]
    assert body["upstream"]["nodes"] == {"dispatcher.apihub-system:8001": 1}
    assert body["plugins"]["proxy-rewrite"]["headers"]["set"] == {
        "X-API-Version-Id": "ver_abc"
    }
    assert body["plugins"]["proxy-rewrite"]["regex_uri"] == ["^/(.*)$", "/dispatch/$1"]


async def test_publish_route_normalizes_path_vars(monkeypatch):
    """{var} → :var（APISIX radixtree 段匹配）。"""
    from apihub_core import apisix_client

    assert apisix_client._normalize_path("/v1/users/{user_id}/orders/{order_id}") == (
        "/v1/users/:user_id/orders/:order_id"
    )


async def test_publish_route_non_2xx_raises_502(monkeypatch):
    class _FakeResp:
        status_code = 401
        text = '{"error": "bad key"}'

        def json(self):
            return {"error": "bad key"}

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def request(self, method, url, **kw):
            return _FakeResp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    from apihub_core import apisix_client

    with pytest.raises(ApiError) as ei:
        await apisix_client.publish_route(
            version_id="ver_x", method="GET", path="/x", base_path="/v1"
        )
    assert ei.value.http_status == 502


async def test_upsert_consumer_puts_admin_consumer(monkeypatch):
    captured = {}

    class _FakeResp:
        status_code = 201

        def json(self):
            return {"ok": True}

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def request(self, method, url, **kw):
            captured["method"] = method
            captured["url"] = url
            captured["json"] = kw.get("json")
            return _FakeResp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    from apihub_core import apisix_client

    await apisix_client.upsert_consumer(key_id="key_abc", key="ak_secret")

    assert captured["method"] == "PUT"
    assert (
        captured["url"]
        == "http://apisix-admin.apihub-ingress:9180/apisix/admin/consumers/key_abc"
    )
    assert captured["json"] == {
        "username": "key_abc",
        "plugins": {"key-auth": {"key": "ak_secret", "header": "X-API-Key"}},
    }


async def test_delete_consumer_deletes(monkeypatch):
    captured = {}

    class _FakeResp:
        status_code = 200

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def request(self, method, url, **kw):
            captured["method"] = method
            captured["url"] = url
            return _FakeResp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    from apihub_core import apisix_client

    await apisix_client.delete_consumer("key_abc")  # 不抛即通过
    assert captured["method"] == "DELETE"
    assert captured["url"].endswith("/apisix/admin/consumers/key_abc")


async def test_delete_consumer_404_is_silent(monkeypatch):
    class _FakeResp:
        status_code = 404
        text = '{"error":"not found"}'

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def request(self, method, url, **kw):
            return _FakeResp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    from apihub_core import apisix_client

    await apisix_client.delete_consumer("key_abc")  # 404 不抛


async def test_publish_route_includes_keyauth_and_ingress_header(monkeypatch):
    from apihub_core.config import get_settings

    monkeypatch.setenv("INGRESS_SHARED_SECRET", "s3cr3t-ingress")
    get_settings.cache_clear()
    captured = {}

    class _FakeResp:
        status_code = 201

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def request(self, method, url, **kw):
            captured["json"] = kw.get("json")
            return _FakeResp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    from apihub_core import apisix_client

    await apisix_client.publish_route(
        version_id="ver_abc", method="GET", path="/u", base_path="/v1"
    )
    plugins = captured["json"]["plugins"]
    assert plugins["key-auth"] == {"header": "X-API-Key"}
    assert plugins["proxy-rewrite"]["headers"]["set"] == {
        "X-API-Version-Id": "ver_abc",
        "X-Ingress-Auth": "s3cr3t-ingress",
    }
    assert "limit-count" not in plugins  # 无 rate_limit
    get_settings.cache_clear()


async def test_publish_route_includes_limit_count_when_rate_limit_set(monkeypatch):
    captured = {}

    class _FakeResp:
        status_code = 201

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def request(self, method, url, **kw):
            captured["json"] = kw.get("json")
            return _FakeResp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    from apihub_core import apisix_client

    await apisix_client.publish_route(
        version_id="ver_abc",
        method="GET",
        path="/u",
        base_path="/v1",
        rate_limit={"count": 10, "window_seconds": 60},
    )
    lc = captured["json"]["plugins"]["limit-count"]
    assert lc == {
        "count": 10,
        "time_window": 60,
        "key": "consumer_name",
        "policy": "local",
        "rejected_code": 429,
    }
