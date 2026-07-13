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
