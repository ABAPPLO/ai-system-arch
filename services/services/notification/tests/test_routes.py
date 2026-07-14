"""HTTP 端点测试 —— httpx ASGITransport 直打 app。"""

import json

from notification import repository as repo_mod


class _MockAsyncClient:
    """替换 routes._test_webhook 中的 httpx.AsyncClient。"""

    def __init__(self, *args, **kwargs):
        self._post_fn = None

    def set_post(self, fn):
        self._post_fn = fn

    async def post(self, url, *, json, headers, timeout=None):
        assert self._post_fn is not None
        return await self._post_fn(url, json=json, headers=headers, timeout=timeout)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass


class _FakeHttpxResponse:
    """轻量 httpx.Response mock。"""

    def __init__(self, status_code: int, json: dict | None = None):
        self.status_code = status_code
        self._json = json or {}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


# ---------- list ----------


class TestListWebhooks:
    async def test_lists_all_for_tenant(self, client, monkeypatch):
        rows = [
            {
                "id": "wh_abc123",
                "url": "https://hooks.example.com/callbacks",
                "events": ["api.call.succeeded", "api.call.failed"],
                "status": "active",
                "created_at": "2026-07-01T00:00:00+00:00",
            },
            {
                "id": "wh_def456",
                "url": "https://other.example.com/hook",
                "events": ["api.call.*"],
                "status": "active",
                "created_at": "2026-07-02T00:00:00+00:00",
            },
        ]
        captured = {}

        async def _list(*, tenant_id):
            captured["tenant_id"] = tenant_id
            return rows

        monkeypatch.setattr(repo_mod, "list_webhooks", _list)

        resp = await client.get("/v1/notification/webhooks")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 2
        assert body[0]["id"] == "wh_abc123"
        assert body[1]["events"] == ["api.call.*"]
        assert captured["tenant_id"] == "t_default"

    async def test_empty_list(self, client, monkeypatch):
        async def _list(*, tenant_id):
            return []

        monkeypatch.setattr(repo_mod, "list_webhooks", _list)
        resp = await client.get("/v1/notification/webhooks")
        assert resp.status_code == 200
        assert resp.json() == []


# ---------- create ----------


class TestCreateWebhook:
    async def test_creates_webhook(self, client, monkeypatch):
        payload = {
            "url": "https://hooks.example.com/callbacks",
            "events": ["api.call.succeeded"],
        }
        captured = {}

        async def _create(*, tenant_id, url, events, secret):
            captured["tenant_id"] = tenant_id
            captured["url"] = url
            captured["events"] = events
            captured["secret"] = secret
            return {"id": "wh_new123", "url": url, "events": events, "status": "active"}

        monkeypatch.setattr(repo_mod, "create_webhook", _create)

        resp = await client.post("/v1/notification/webhooks", json=payload)
        assert resp.status_code == 201
        body = resp.json()
        assert body["id"] == "wh_new123"
        assert body["status"] == "active"
        assert captured["tenant_id"] == "t_default"
        assert captured["url"] == payload["url"]
        assert captured["events"] == payload["events"]
        assert captured["secret"] is None

    async def test_creates_with_secret(self, client, monkeypatch):
        payload = {
            "url": "https://hooks.example.com/callbacks",
            "events": ["api.call.failed"],
            "secret": "my-secret-key",
        }
        captured = {}

        async def _create(*, tenant_id, url, events, secret):
            captured["secret"] = secret
            return {"id": "wh_secret1", "url": url, "events": events, "status": "active"}

        monkeypatch.setattr(repo_mod, "create_webhook", _create)

        resp = await client.post("/v1/notification/webhooks", json=payload)
        assert resp.status_code == 201
        assert captured["secret"] == "my-secret-key"

    async def test_rejects_empty_events(self, client):
        resp = await client.post(
            "/v1/notification/webhooks",
            json={"url": "https://example.com/hook", "events": []},
        )
        assert resp.status_code == 422

    async def test_rejects_invalid_url(self, client):
        resp = await client.post(
            "/v1/notification/webhooks",
            json={"url": "http", "events": ["api.call.succeeded"]},
        )
        assert resp.status_code == 422


# ---------- update ----------


class TestUpdateWebhook:
    async def test_updates_fields(self, client, monkeypatch):
        captured = {}

        async def _update(*, tenant_id, webhook_id, updates):
            captured["webhook_id"] = webhook_id
            captured["updates"] = updates
            return {"id": webhook_id, "url": "https://updated.example.com/hook",
                    "events": ["api.call.succeeded"], "status": "inactive",
                    "created_at": "2026-07-01T00:00:00+00:00"}

        monkeypatch.setattr(repo_mod, "update_webhook", _update)

        resp = await client.put(
            "/v1/notification/webhooks/wh_abc123",
            json={"url": "https://updated.example.com/hook", "status": "inactive"},
        )
        assert resp.status_code == 200
        assert captured["webhook_id"] == "wh_abc123"
        assert captured["updates"] == {"url": "https://updated.example.com/hook",
                                       "status": "inactive"}

    async def test_rejects_empty_update(self, client):
        resp = await client.put(
            "/v1/notification/webhooks/wh_abc123", json={}
        )
        assert resp.status_code == 400
        assert "no fields" in resp.text.lower()

    async def test_partial_update_events_only(self, client, monkeypatch):
        captured = {}

        async def _update(*, tenant_id, webhook_id, updates):
            captured["updates"] = updates
            return {"id": webhook_id, "url": "https://example.com/hook",
                    "events": updates["events"], "status": "active",
                    "created_at": "2026-07-01T00:00:00+00:00"}

        monkeypatch.setattr(repo_mod, "update_webhook", _update)

        resp = await client.put(
            "/v1/notification/webhooks/wh_abc123",
            json={"events": ["api.call.*"]},
        )
        assert resp.status_code == 200
        assert captured["updates"] == {"events": ["api.call.*"]}

    async def test_update_not_found(self, client, monkeypatch):
        async def _update(*, tenant_id, webhook_id, updates):
            from apihub_core.errors import ApiError, ErrorCode
            raise ApiError(ErrorCode.NOT_FOUND, "webhook not found")

        monkeypatch.setattr(repo_mod, "update_webhook", _update)

        resp = await client.put(
            "/v1/notification/webhooks/wh_nonexistent",
            json={"url": "https://example.com/hook"},
        )
        assert resp.status_code == 404


# ---------- delete ----------


class TestDeleteWebhook:
    async def test_deletes_webhook(self, client, monkeypatch):
        captured = {}

        async def _delete(*, tenant_id, webhook_id):
            captured["webhook_id"] = webhook_id
            captured["tenant_id"] = tenant_id

        monkeypatch.setattr(repo_mod, "delete_webhook", _delete)

        resp = await client.delete("/v1/notification/webhooks/wh_abc123")
        assert resp.status_code == 200
        assert resp.json() == {"status": "deleted"}
        assert captured["webhook_id"] == "wh_abc123"
        assert captured["tenant_id"] == "t_default"

    async def test_delete_not_found(self, client, monkeypatch):
        async def _delete(*, tenant_id, webhook_id):
            from apihub_core.errors import ApiError, ErrorCode
            raise ApiError(ErrorCode.NOT_FOUND, "webhook not found")

        monkeypatch.setattr(repo_mod, "delete_webhook", _delete)

        resp = await client.delete("/v1/notification/webhooks/wh_nonexistent")
        assert resp.status_code == 404


# ---------- test (ping) ----------


class TestTestWebhook:
    def _patch_httpx_post(self, monkeypatch, handler):
        """替换 httpx.AsyncClient 让 routes._test_webhook 用 mock。

        _test_webhook 在函数体内 `import httpx`，所以 httpx 指向全局模块对象。
        替换 httpx.AsyncClient 为返回 mock 的工厂函数即可。
        """
        import httpx
        mock = _MockAsyncClient()
        mock.set_post(handler)
        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: mock)
        return mock

    async def test_ping_success(self, client, monkeypatch):
        hooks = [
            {"id": "wh_abc123", "url": "https://example.com/hook",
             "events": ["api.call.succeeded"], "status": "active", "secret": ""},
        ]

        async def _list(*, tenant_id):
            return hooks

        monkeypatch.setattr(repo_mod, "list_webhooks", _list)

        async def _post(url, *, json, headers, timeout=None):
            return _FakeHttpxResponse(200, json={"ok": True})

        self._patch_httpx_post(monkeypatch, _post)

        resp = await client.post("/v1/notification/webhooks/wh_abc123/test")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["status_code"] == 200

    async def test_ping_target_fails(self, client, monkeypatch):
        hooks = [
            {"id": "wh_abc123", "url": "https://example.com/hook",
             "events": ["api.call.succeeded"], "status": "active", "secret": None},
        ]

        async def _list(*, tenant_id):
            return hooks

        monkeypatch.setattr(repo_mod, "list_webhooks", _list)

        async def _post(url, *, json, headers, timeout=None):
            return _FakeHttpxResponse(500)

        self._patch_httpx_post(monkeypatch, _post)

        resp = await client.post("/v1/notification/webhooks/wh_abc123/test")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False
        # 5xx → success=False
        assert body["status_code"] == 500

    async def test_ping_connection_error(self, client, monkeypatch):
        hooks = [
            {"id": "wh_abc123", "url": "https://unreachable.example.com/hook",
             "events": ["api.call.succeeded"], "status": "active", "secret": None},
        ]

        async def _list(*, tenant_id):
            return hooks

        monkeypatch.setattr(repo_mod, "list_webhooks", _list)
        import httpx

        async def _post(url, *, json, headers, timeout=None):
            raise httpx.RequestError("connection refused")

        self._patch_httpx_post(monkeypatch, _post)

        resp = await client.post("/v1/notification/webhooks/wh_abc123/test")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False
        assert body["error"] == "connection refused"

    async def test_ping_unknown_webhook(self, client, monkeypatch):
        async def _list(*, tenant_id):
            return []

        monkeypatch.setattr(repo_mod, "list_webhooks", _list)

        resp = await client.post("/v1/notification/webhooks/wh_nonexistent/test")
        assert resp.status_code == 404


# ---------- health ----------


class TestHealth:
    async def test_health(self, client):
        resp = await client.get("/v1/notification/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "service": "notification"}
