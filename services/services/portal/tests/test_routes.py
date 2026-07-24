"""portal-bff 路由单测（app/key 走转发，mock httpx；其余 mock repository）。"""


async def test_create_app_forwards_to_auth(client, monkeypatch):
    """POST /v1/portal/apps 转发用户 JWT 到 auth /v1/apps，不再直写 PG。"""
    import httpx as _httpx

    captured = {}

    class _FakeResp:
        status_code = 200  # auth POST /v1/apps 返回 200

        def json(self):
            return {
                "id": "app_new",
                "name": "my app",
                "tenant_id": "external-public",
                "type": "external",
                "status": "active",
            }

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

    monkeypatch.setattr(_httpx, "AsyncClient", _FakeClient)

    r = await client.post("/v1/portal/apps", json={"name": "my app", "type": "external"})
    assert r.status_code == 201  # portal 契约 201
    assert r.json()["id"] == "app_new"
    # 转发到 auth /v1/apps，无 /v1/v1/ 双前缀
    assert captured["url"] == "http://auth.apihub-system/v1/apps", captured["url"]
    assert captured["method"] == "POST"
    # 用户 JWT 原样转发
    assert captured["headers"]["Authorization"] == "Bearer eyJ.test.token"
    assert captured["json"] == {"name": "my app", "type": "external"}


async def test_list_apps_forwards_to_auth(client, monkeypatch):
    """GET /v1/portal/apps 转发 auth /v1/apps。"""
    import httpx as _httpx

    captured = {}

    class _FakeResp:
        status_code = 200

        def json(self):
            return [
                {
                    "id": "app_a",
                    "name": "A",
                    "tenant_id": "external-public",
                    "type": "external",
                    "status": "active",
                }
            ]

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def request(self, method, url, **kw):
            captured["url"] = url
            captured["method"] = method
            return _FakeResp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(_httpx, "AsyncClient", _FakeClient)

    r = await client.get("/v1/portal/apps")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["id"] == "app_a"
    assert captured["url"] == "http://auth.apihub-system/v1/apps"
    assert captured["method"] == "GET"


async def test_portal_funnel_forwards_to_trace(client, monkeypatch):
    """GET /v1/portal/analytics/funnel 薄转发 trace-svc，透传 JWT（前端不得直连 trace）。"""
    import httpx as _httpx

    captured = {}

    class _FakeResp:
        status_code = 200

        def json(self):
            return [{"trace_id": "t1", "step_count": 2, "steps": [{"api_id": "a", "path": "/x"}]}]

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def get(self, url, **kw):
            captured["url"] = url
            captured["headers"] = kw.get("headers")
            return _FakeResp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(_httpx, "AsyncClient", _FakeClient)

    r = await client.get("/v1/portal/analytics/funnel")
    assert r.status_code == 200
    assert captured["url"] == "http://trace.apihub-system/v1/trace/analytics/funnel", captured[
        "url"
    ]
    # 用户 JWT 原样透传给 trace-svc（由其做租户隔离）
    assert captured["headers"]["Authorization"] == "Bearer eyJ.test.token"
    assert r.json()[0]["trace_id"] == "t1"


async def test_portal_cooccurrence_forwards_to_trace(client, monkeypatch):
    """GET /v1/portal/analytics/co-occurrence 薄转发 trace-svc + 透传 query。"""
    import httpx as _httpx

    captured = {}

    class _FakeResp:
        status_code = 200

        def json(self):
            return [
                {
                    "api_a": "a",
                    "path_a": "/x",
                    "api_b": "b",
                    "path_b": "/y",
                    "pair_count": 5,
                }
            ]

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def get(self, url, **kw):
            captured["url"] = url
            captured["params"] = kw.get("params")
            return _FakeResp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(_httpx, "AsyncClient", _FakeClient)

    r = await client.get("/v1/portal/analytics/co-occurrence?since=2026-01-01&min_pairs=2")
    assert r.status_code == 200
    assert (
        captured["url"] == "http://trace.apihub-system/v1/trace/analytics/co-occurrence"
    ), captured["url"]
    # query 透传给 trace-svc（dict(QueryParams) 值均为 str）
    assert captured["params"] == {"since": "2026-01-01", "min_pairs": "2"}, captured["params"]
    assert r.json()[0]["pair_count"] == 5


async def test_create_api_key_forwards_and_maps_prefix(client, monkeypatch):
    """POST /v1/portal/apps/{id}/api-keys 转发 auth，并把 display_prefix→key_prefix。"""
    import httpx as _httpx

    captured = {}

    class _FakeResp:
        status_code = 200

        def json(self):
            return {
                "id": "key_new",
                "app_id": "app_x",
                "name": "prod key",
                "scopes": [],
                "api_key": "ak_supersecret",
                "display_prefix": "ak_abcd12",
                "expires_at": None,
                "created_at": "2026-07-16T00:00:00",
            }

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def request(self, method, url, **kw):
            captured["url"] = url
            captured["json"] = kw.get("json")
            return _FakeResp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(_httpx, "AsyncClient", _FakeClient)

    r = await client.post("/v1/portal/apps/app_x/api-keys", json={"name": "prod key"})
    assert r.status_code == 201  # portal 契约 201
    body = r.json()
    assert body["api_key"] == "ak_supersecret"
    assert body["key_prefix"] == "ak_abcd12"  # 映射自 auth display_prefix
    assert "display_prefix" not in body  # portal 不暴露 auth 原字段
    assert captured["url"] == "http://auth.apihub-system/v1/apps/app_x/api-keys"
    assert captured["json"] == {"name": "prod key"}


async def test_list_api_keys_forwards_to_auth(client, monkeypatch):
    import httpx as _httpx

    captured = {}

    class _FakeResp:
        status_code = 200

        def json(self):
            return [
                {
                    "id": "key_1",
                    "app_id": "app_x",
                    "name": "k",
                    "scopes": [],
                    "display_prefix": "ak_ab",
                    "status": "active",
                    "last_used_at": None,
                    "expires_at": None,
                    "created_at": "2026-07-16T00:00:00",
                    "revoked_at": None,
                    "signing": True,
                }
            ]

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

    monkeypatch.setattr(_httpx, "AsyncClient", _FakeClient)
    r = await client.get("/v1/portal/apps/app_x/api-keys")
    assert r.status_code == 200
    assert r.json()[0]["signing"] is True
    assert captured["method"] == "GET"
    assert captured["url"] == "http://auth.apihub-system/v1/apps/app_x/api-keys"


async def test_revoke_api_key_forwards_to_auth(client, monkeypatch):
    import httpx as _httpx

    captured = {}

    class _FakeResp:
        status_code = 200

        def json(self):
            return {"id": "key_1", "status": "revoked"}

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

    monkeypatch.setattr(_httpx, "AsyncClient", _FakeClient)
    r = await client.delete("/v1/portal/api-keys/key_1")
    assert r.status_code == 200
    assert captured["method"] == "DELETE"
    assert captured["url"] == "http://auth.apihub-system/v1/api-keys/key_1"


async def test_rotate_api_key_forwards_to_auth(client, monkeypatch):
    import httpx as _httpx

    captured = {}

    class _FakeResp:
        status_code = 200

        def json(self):
            return {"key_id": "key_1", "hmac_secret": "new_secret_xyz"}

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

    monkeypatch.setattr(_httpx, "AsyncClient", _FakeClient)
    r = await client.post("/v1/portal/api-keys/key_1/hmac-secret/rotate")
    assert r.status_code == 200
    assert r.json()["hmac_secret"] == "new_secret_xyz"
    assert captured["method"] == "POST"
    assert captured["url"] == "http://auth.apihub-system/v1/api-keys/key_1/hmac-secret/rotate"


async def test_auth_endpoints_skip_auth_paths(monkeypatch):
    """身份端点在 skip_auth_paths 里 → 无 Authorization 也能到达（不会被 middleware 拦成 401）。

    mock 掉真实 httpx 转发（避免命中 host proxy / 真实 auth 服务），只验证 middleware 放行。
    """
    import httpx as _httpx
    from httpx import ASGITransport
    from portal.main import app

    # 注意：portal_routes.httpx IS 全局 httpx 模块 —— patch 会全局生效。
    # 所以先捕获真实的 AsyncClient 类给 test transport 用。
    real_async_client = _httpx.AsyncClient

    class _FakeResp:
        def __init__(self):
            self.status_code = 200

        def json(self):
            return {"ok": True}

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def request(self, method, url, **kw):
            assert url.endswith("/v1/auth/login"), url
            return _FakeResp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(_httpx, "AsyncClient", _FakeClient)

    async with real_async_client(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as no_auth_client:
        # 不带 Authorization 头：若 middleware 拦下会是 401/422；skip_auth 放行后
        # 转发走 _FakeClient → 200
        r = await no_auth_client.post("/v1/portal/auth/login", json={})
    assert r.status_code == 200
    assert r.json() == {"ok": True}


async def test_portal_account_endpoints_forward_with_v1(client, monkeypatch):
    """M4 回归：4 个 account/consent handler 必须转发到 /v1/auth/...（旧实现缺 /v1 → 404）。"""
    import httpx as _httpx

    captured = {}

    class _FakeResp:
        status_code = 200

        def json(self):
            return {"ok": True}

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def request(self, method, url, **kw):
            captured.setdefault("calls", []).append((method, url))
            return _FakeResp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(_httpx, "AsyncClient", _FakeClient)

    await client.delete("/v1/portal/auth/account")
    await client.get("/v1/portal/auth/account/export")
    await client.get("/v1/portal/auth/consent")
    await client.post("/v1/portal/auth/consent/withdraw")

    methods_paths = captured["calls"]
    expected = [
        ("DELETE", "http://auth.apihub-system/v1/auth/account"),
        ("GET", "http://auth.apihub-system/v1/auth/account/export"),
        ("GET", "http://auth.apihub-system/v1/auth/consent"),
        ("POST", "http://auth.apihub-system/v1/auth/consent/withdraw"),
    ]
    assert methods_paths == expected, methods_paths
    assert all("/v1/v1/" not in u for _, u in methods_paths)


async def test_forward_composes_correct_auth_url(monkeypatch):
    """_forward 必须拼出正确的 absolute URL，不能出现 /v1/v1/（Task4 review Finding 1）。

    旧 test 只断言 url.endswith('/v1/auth/login') —— /v1/v1/auth/login 也满足，盲区。
    本测试捕获完整 absolute URL，断言精确等于 http://auth.apihub-system/v1/auth/login。
    """
    import httpx as _httpx
    from httpx import ASGITransport
    from portal.main import app

    real_async_client = _httpx.AsyncClient
    captured = {}

    class _FakeResp:
        status_code = 200

        def json(self):
            return {"ok": True}

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def request(self, method, url, **kw):
            captured["method"] = method
            captured["url"] = url  # 完整 absolute URL
            return _FakeResp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(_httpx, "AsyncClient", _FakeClient)

    async with real_async_client(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as no_auth_client:
        r = await no_auth_client.post("/v1/portal/auth/login", json={})
    assert r.status_code == 200
    # 关键断言：无 /v1/v1/ 双前缀
    assert "/v1/v1/" not in captured["url"], captured["url"]
    assert captured["url"] == "http://auth.apihub-system/v1/auth/login", captured["url"]
    assert captured["method"] == "POST"


# ========== API 目录 + 在线调试（Task 4）==========


async def test_list_portal_apis(client, monkeypatch):
    """GET /v1/portal/apis 返回过滤/分页后的 API 列表。"""
    from portal.models import PortalApiItem, PortalApiListResponse

    async def fake_list(**kw):
        return PortalApiListResponse(
            items=[
                PortalApiItem(
                    api_id="api_1",
                    name="Test API",
                    category="test",
                    tags=["foo"],
                    base_path="/test",
                    visibility="public",
                    backend_type="http",
                    version="v1",
                    updated_at="2026-07-13T00:00:00",
                )
            ],
            total=1,
            limit=50,
            offset=0,
            categories=["test"],
            tags=["foo"],
        )

    monkeypatch.setattr("portal.routes.repository.list_portal_apis", fake_list)

    r = await client.get("/v1/portal/apis?search=test&category=test&tag=foo")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["name"] == "Test API"
    assert body["categories"] == ["test"]
    assert body["tags"] == ["foo"]


async def test_get_api_detail(client, monkeypatch):
    """GET /v1/portal/apis/{id} 返回 API 详情 + 版本列表。"""
    from portal.models import PortalApiDetail, PortalVersionItem

    async def fake_detail(api_id):
        return PortalApiDetail(
            api_id=api_id,
            name="Detail API",
            category="test",
            tags=[],
            base_path="/test",
            visibility="public",
            api_status="published",
            versions=[
                PortalVersionItem(
                    version_id="ver_1",
                    version="v1",
                    method="GET",
                    path="/echo",
                    backend_type="http",
                    status="published",
                    request_schema={"type": "object"},
                ),
            ],
        )

    monkeypatch.setattr("portal.routes.repository.get_api_detail", fake_detail)

    r = await client.get("/v1/portal/apis/api_1")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Detail API"
    assert len(body["versions"]) == 1
    assert body["versions"][0]["version"] == "v1"


async def test_try_api_success(client, monkeypatch):
    """POST /v1/portal/try 成功返回后端响应 + 延迟。"""
    from portal.models import TryResponse

    async def fake_try(payload):
        return TryResponse(
            status=200,
            headers={"content-type": "application/json"},
            body={"ok": True},
            latency_ms=42,
        )

    monkeypatch.setattr("portal.routes.repository.try_api", fake_try)

    r = await client.post(
        "/v1/portal/try",
        json={
            "api_id": "api_1",
            "method": "GET",
            "api_key": "ak_test_valid",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == 200
    assert body["body"] == {"ok": True}
    assert body["latency_ms"] == 42
    assert body["error"] is None


async def test_try_api_key_invalid(client, monkeypatch):
    """POST /v1/portal/try 在 API Key 无效时返回 401 error。"""
    from portal.models import TryResponse

    async def fake_try(payload):
        return TryResponse(status=401, error="API Key 无效")

    monkeypatch.setattr("portal.routes.repository.try_api", fake_try)

    r = await client.post(
        "/v1/portal/try",
        json={
            "api_id": "api_1",
            "method": "GET",
            "api_key": "ak_bad",
        },
    )
    assert r.status_code == 200  # try 端点始终 200
    body = r.json()
    assert body["status"] == 401
    assert body["error"] is not None


async def test_try_api_backend_timeout(client, monkeypatch):
    """POST /v1/portal/try 在后端超时时返回 504 error + latency。"""
    from portal.models import TryResponse

    async def fake_try(payload):
        return TryResponse(status=504, error="后端响应超时", latency_ms=30000)

    monkeypatch.setattr("portal.routes.repository.try_api", fake_try)

    r = await client.post(
        "/v1/portal/try",
        json={
            "api_id": "api_1",
            "method": "GET",
            "api_key": "ak_test",
            "timeout_ms": 100,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == 504
    assert body["latency_ms"] == 30000


# ========== 用量/计费（Task 7）==========


async def test_portal_plans(client, monkeypatch):
    """GET /v1/portal/plans 返回 plan 列表。"""
    from portal.models import PlanInfo

    async def fake_plans():
        return [
            PlanInfo(
                code="free",
                name="Free",
                price_cents=0,
                quota_included={},
                rate_limits={},
                sort_order=1,
            )
        ]

    monkeypatch.setattr("portal.routes.repository.list_plans", fake_plans)
    r = await client.get("/v1/portal/plans")
    assert r.status_code == 200
    assert r.json()[0]["code"] == "free"


async def test_portal_subscription(client, monkeypatch):
    """GET /v1/portal/subscription 返回当前 plan。"""
    from portal.models import SubscriptionInfo

    async def fake_sub(tenant_id):
        return SubscriptionInfo(
            plan_code="free",
            plan_name="Free",
            period_start="2026-01-01",
            period_end="2999-12-31",
            status="active",
            auto_renew=True,
        )

    monkeypatch.setattr("portal.routes.repository.get_subscription", fake_sub)
    r = await client.get("/v1/portal/subscription")
    assert r.status_code == 200
    assert r.json()["plan_code"] == "free"


async def test_portal_usage(client, monkeypatch):
    """GET /v1/portal/usage 返回用量概览。"""

    async def fake_usage(tenant_id):
        return {
            "tenant_id": tenant_id,
            "month": "2026-07",
            "plan": {"code": "free"},
            "daily_usage": [],
            "total_calls": 0,
            "total_tokens": 0,
            "remaining_calls_today": 1000,
        }

    monkeypatch.setattr("portal.routes.repository.get_billing_summary", fake_usage)
    r = await client.get("/v1/portal/usage")
    assert r.status_code == 200
    assert r.json()["remaining_calls_today"] == 1000
