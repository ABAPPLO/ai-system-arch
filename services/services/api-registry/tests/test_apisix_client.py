"""apisix_client 单测 —— stub httpx，断言 Admin API 请求形状。"""

import httpx
import pytest
from apihub_core.errors import ApiError


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    from apihub_core.config import get_settings

    get_settings.cache_clear()
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
    from api_registry import apisix_client

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
    from api_registry import apisix_client

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
    from api_registry import apisix_client

    with pytest.raises(ApiError) as ei:
        await apisix_client.publish_route(
            version_id="ver_x", method="GET", path="/x", base_path="/v1"
        )
    assert ei.value.http_status == 502
