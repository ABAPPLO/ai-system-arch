"""portal-bff 路由单测（mock repository，不触 DB 栈）。"""


async def test_create_app_uses_caller_tenant(client, monkeypatch):
    """POST /v1/portal/apps 必须用 JWT 上下文里的 tenant_id（而非请求体）。"""
    captured = {}

    async def fake_create(*, tenant_id, name, app_type):
        captured["tenant_id"] = tenant_id
        return {
            "id": "app_x",
            "name": name,
            "tenant_id": tenant_id,
            "type": app_type,
            "status": "active",
        }

    monkeypatch.setattr("portal.routes.repository.create_app_for_user", fake_create)

    r = await client.post(
        "/v1/portal/apps", json={"name": "my app", "type": "external"}
    )
    assert r.status_code == 201
    body = r.json()
    assert body["id"] == "app_x"
    assert body["tenant_id"] == "external-public"
    # 关键断言：传给 repository 的 tenant_id 来自 JWT ctx，不是请求体
    assert captured["tenant_id"] == "external-public"


async def test_list_apps(client, monkeypatch):
    """GET /v1/portal/apps 返回当前租户的 app 列表。"""
    async def fake_list(*, tenant_id):
        return []

    monkeypatch.setattr("portal.routes.repository.list_apps_for_user", fake_list)
    r = await client.get("/v1/portal/apps")
    assert r.status_code == 200
    assert r.json() == []


async def test_create_api_key(client, monkeypatch):
    """POST /v1/portal/apps/{id}/api-keys 走 repository 并返回明文 key（仅此一次）。"""
    captured = {}

    async def fake_create_key(*, tenant_id, app_id, name):
        captured.update(tenant_id=tenant_id, app_id=app_id, name=name)
        return {
            "id": "key_x",
            "app_id": app_id,
            "name": name,
            "key_prefix": "ak_abcd12",
            "api_key": "ak_supersecret",
        }

    monkeypatch.setattr(
        "portal.routes.repository.create_api_key_for_app", fake_create_key
    )

    r = await client.post(
        "/v1/portal/apps/app_x/api-keys", json={"name": "prod key"}
    )
    assert r.status_code == 201
    body = r.json()
    assert body["api_key"] == "ak_supersecret"
    assert captured["app_id"] == "app_x"
    assert captured["tenant_id"] == "external-public"


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


async def test_create_api_key_for_app_rejects_foreign_app(monkeypatch):
    """repository.create_api_key_for_app 必须校验 app 归属（Task4 review Finding 2）。

    db_session 已 SET LOCAL app.tenant_id=caller，RLS 自动过滤跨租户 app。
    fetchval 返回 None（app 不在 caller 租户）→ 抛 ApiError NOT_FOUND，且不执行 INSERT。
    """
    import pytest
    from apihub_core.errors import ApiError, ErrorCode
    from portal import repository

    class _FakeConn:
        async def fetchval(self, query, *args):
            # RLS 已过滤跨租户 app → 查不到
            return None

        async def execute(self, *a, **kw):
            raise AssertionError("不得在归属校验失败时 INSERT api_key")

    class _FakeCM:
        async def __aenter__(self):
            return _FakeConn()

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(repository.db, "db_session", lambda: _FakeCM())

    with pytest.raises(ApiError) as ei:
        await repository.create_api_key_for_app(
            tenant_id="t_foreign", app_id="app_other_tenant", name="leaked-key"
        )
    assert ei.value.code == ErrorCode.NOT_FOUND
    assert ei.value.http_status == 404


# ========== API 目录 + 在线调试（Task 4）==========


async def test_list_portal_apis(client, monkeypatch):
    """GET /v1/portal/apis 返回过滤/分页后的 API 列表。"""
    from portal.models import PortalApiListResponse, PortalApiItem

    async def fake_list(**kw):
        return PortalApiListResponse(
            items=[
                PortalApiItem(
                    api_id="api_1", name="Test API", category="test",
                    tags=["foo"], base_path="/test", visibility="public",
                    backend_type="http", version="v1", updated_at="2026-07-13T00:00:00",
                )
            ],
            total=1, limit=50, offset=0,
            categories=["test"], tags=["foo"],
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
            api_id=api_id, name="Detail API", category="test",
            tags=[], base_path="/test", visibility="public",
            api_status="published",
            versions=[
                PortalVersionItem(
                    version_id="ver_1", version="v1", method="GET",
                    path="/echo", backend_type="http", status="published",
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
        return [PlanInfo(code="free", name="Free", price_cents=0, quota_included={}, rate_limits={}, sort_order=1)]
    monkeypatch.setattr("portal.routes.repository.list_plans", fake_plans)
    r = await client.get("/v1/portal/plans")
    assert r.status_code == 200
    assert r.json()[0]["code"] == "free"


async def test_portal_subscription(client, monkeypatch):
    """GET /v1/portal/subscription 返回当前 plan。"""
    from portal.models import SubscriptionInfo
    async def fake_sub(tenant_id):
        return SubscriptionInfo(plan_code="free", plan_name="Free", period_start="2026-01-01",
                                period_end="2999-12-31", status="active", auto_renew=True)
    monkeypatch.setattr("portal.routes.repository.get_subscription", fake_sub)
    r = await client.get("/v1/portal/subscription")
    assert r.status_code == 200
    assert r.json()["plan_code"] == "free"


async def test_portal_usage(client, monkeypatch):
    """GET /v1/portal/usage 返回用量概览。"""
    async def fake_usage(tenant_id):
        return {"tenant_id": tenant_id, "month": "2026-07", "plan": {"code": "free"},
                "daily_usage": [], "total_calls": 0, "total_tokens": 0, "remaining_calls_today": 1000}
    monkeypatch.setattr("portal.routes.repository.get_billing_summary", fake_usage)
    r = await client.get("/v1/portal/usage")
    assert r.status_code == 200
    assert r.json()["remaining_calls_today"] == 1000
