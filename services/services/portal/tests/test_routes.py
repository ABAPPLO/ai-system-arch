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
