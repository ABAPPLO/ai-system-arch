"""consumer 单元测试 —— Webhook 推送逻辑。"""

import contextlib


class _MockAsyncClient:
    """轻量 mock for httpx.AsyncClient — 完全替换 consumer 模块的 import。"""

    def __init__(self, *args, **kwargs):
        self._post_fn = None

    def set_post(self, fn):
        self._post_fn = fn

    async def post(self, url, *, content, headers, timeout=None):  # noqa: ASYNC109 -- mock matches httpx AsyncClient.post signature
        assert self._post_fn is not None, "call set_post() before test"
        return await self._post_fn(url, content=content, headers=headers, timeout=timeout)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass


# ---------- _deliver ----------


class TestDeliver:
    """测试 _deliver 的 HTTP 投递逻辑。"""

    async def _run_deliver(self, monkeypatch, post_fn, url="https://example.com/hook",
                           payload=None, secret="s"):
        """Helper：替换 consumer 模块的 httpx.AsyncClient 后调用 _deliver。"""
        if payload is None:
            payload = {"event": "test"}
        from notification import consumer as consumer_mod

        mock = _MockAsyncClient()
        mock.set_post(post_fn)
        monkeypatch.setattr(consumer_mod.httpx, "AsyncClient", lambda *a, **kw: mock)

        from notification.consumer import _deliver
        return await _deliver(url, payload, secret)

    async def test_deliver_success(self, monkeypatch):
        captured = {}

        async def _post(url, *, content, headers, timeout=None):  # noqa: ASYNC109 -- mock matches httpx post signature
            captured["url"] = url
            captured["content"] = content
            captured["headers"] = headers
            return _FakeResponse(200)

        ok = await self._run_deliver(monkeypatch, _post)
        assert ok is True
        assert captured["url"] == "https://example.com/hook"

    async def test_deliver_no_secret(self, monkeypatch):
        captured = {}

        async def _post(url, *, content, headers, timeout=None):  # noqa: ASYNC109 -- mock matches httpx post signature
            captured["headers"] = headers
            return _FakeResponse(200)

        await self._run_deliver(monkeypatch, _post, secret="")
        assert captured["headers"]["X-Webhook-Signature"] == ""

    async def test_deliver_server_error(self, monkeypatch):
        async def _post(url, *, content, headers, timeout=None):  # noqa: ASYNC109 -- mock matches httpx post signature
            return _FakeResponse(503)

        ok = await self._run_deliver(monkeypatch, _post)
        assert ok is False

    async def test_deliver_client_error_still_ok(self, monkeypatch):
        """4xx 不看作投递失败（调用方明确拒绝），和 2xx 一样视为成功。"""
        async def _post(url, *, content, headers, timeout=None):  # noqa: ASYNC109 -- mock matches httpx post signature
            return _FakeResponse(404)

        ok = await self._run_deliver(monkeypatch, _post)
        assert ok is True  # 4xx < 500 → 视为成功

    async def test_deliver_network_error(self, monkeypatch):
        import httpx

        async def _post(url, *, content, headers, timeout=None):  # noqa: ASYNC109 -- mock matches httpx post signature
            raise httpx.RequestError("timeout")

        ok = await self._run_deliver(monkeypatch, _post)
        assert ok is False

    async def test_deliver_hmac_signature(self, monkeypatch):
        """验证 HMAC 签名正确性（已知 secret + payload 的期望 sig）。"""
        import hashlib
        import hmac

        captured = {}
        secret = "my-secret-123"

        async def _post(url, *, content, headers, timeout=None):  # noqa: ASYNC109 -- mock matches httpx post signature
            captured["content"] = content
            captured["sig"] = headers.get("X-Webhook-Signature", "")
            return _FakeResponse(200)

        await self._run_deliver(monkeypatch, _post, payload={"msg": "hello"}, secret=secret)
        expected_sig = hmac.new(secret.encode(), captured["content"],
                                hashlib.sha256).hexdigest()
        assert captured["sig"] == expected_sig


class _FakeResponse:
    """轻量 HTTP 响应 mock。"""

    def __init__(self, status_code: int):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


# ---------- _get_active_webhooks ----------


class _FakeConn:
    """模拟 asyncpg 连接的 fetch 方法。"""
    def __init__(self, rows):
        self._rows = rows
        self.captured_sql = []

    async def fetch(self, sql, *args):
        self.captured_sql.append(sql)
        return self._rows


class TestGetActiveWebhooks:
    def _patch_db(self, monkeypatch, rows):
        """替换 db.admin_db_session 为返回 _FakeConn 的 async context manager。"""
        conn = _FakeConn(rows)

        @contextlib.asynccontextmanager
        async def _fake_session():
            yield conn

        monkeypatch.setattr("apihub_core.db.admin_db_session", _fake_session)
        return conn

    async def test_fetches_active_only(self, monkeypatch):
        from notification import consumer as consumer_mod

        rows = [
            {"id": "wh_1", "tenant_id": "t1", "url": "https://a.com/hook",
             "events": ["api.call.succeeded"], "secret": ""},
            {"id": "wh_2", "tenant_id": "t2", "url": "https://b.com/hook",
             "events": ["api.call.failed"], "secret": "s1"},
        ]
        conn = self._patch_db(monkeypatch, rows)

        result = await consumer_mod._get_active_webhooks()
        assert len(result) == 2
        assert result[0]["id"] == "wh_1"
        assert result[1]["secret"] == "s1"
        assert any("status = 'active'" in sql for sql in conn.captured_sql)

    async def test_returns_empty_list(self, monkeypatch):
        from notification import consumer as consumer_mod

        self._patch_db(monkeypatch, [])

        result = await consumer_mod._get_active_webhooks()
        assert result == []


# ---------- process_event ----------


class TestProcessEvent:
    def _patch(self, monkeypatch, hooks, deliver_fn=None):
        """Patch both _get_active_webhooks and _deliver. 返回 delivered 列表。"""
        async def _get_hooks():
            return hooks

        monkeypatch.setattr("notification.consumer._get_active_webhooks", _get_hooks)

        delivered = []

        if deliver_fn is None:
            async def _default_deliver(url, payload, secret):
                delivered.append(url)
                return True
            deliver_fn = _default_deliver

        monkeypatch.setattr("notification.consumer._deliver", deliver_fn)
        return delivered

    async def test_matches_specific_event(self, monkeypatch):
        from notification.consumer import process_event

        hooks = [
            {"id": "wh_1", "tenant_id": "t1", "url": "https://a.com/hook",
             "events": ["api.call.succeeded"], "secret": ""},
            {"id": "wh_2", "tenant_id": "t1", "url": "https://b.com/hook",
             "events": ["api.call.failed"], "secret": ""},
        ]

        async def _get_hooks():
            return hooks

        monkeypatch.setattr("notification.consumer._get_active_webhooks", _get_hooks)

        real_delivered = []

        async def _recording_deliver(url, payload, secret):
            real_delivered.append((url, payload["event"], secret))
            return True

        monkeypatch.setattr("notification.consumer._deliver", _recording_deliver)

        await process_event({"allowed": True, "api_id": "api-1", "app_id": "app-1"})

        # 只匹配 api.call.succeeded
        assert len(real_delivered) == 1
        assert real_delivered[0][0] == "https://a.com/hook"
        assert real_delivered[0][1] == "api.call.succeeded"

    async def test_matches_wildcard(self, monkeypatch):
        from notification.consumer import process_event

        hooks = [
            {"id": "wh_1", "tenant_id": "t1", "url": "https://a.com/hook",
             "events": ["api.call.*"], "secret": ""},
        ]

        delivered = []

        async def _recording_deliver(url, payload, secret):
            delivered.append(url)
            return True

        self._patch(monkeypatch, hooks, _recording_deliver)

        await process_event({"allowed": False, "api_id": "api-1"})
        assert len(delivered) == 1
        assert delivered[0] == "https://a.com/hook"

    async def test_no_matching_webhooks(self, monkeypatch):
        from notification.consumer import process_event

        hooks = [
            {"id": "wh_1", "tenant_id": "t1", "url": "https://a.com/hook",
             "events": ["api.call.failed"], "secret": ""},
        ]

        delivered = self._patch(monkeypatch, hooks)
        monkeypatch.setattr("notification.consumer._deliver",
                            lambda url, payload, secret: delivered.append(url) or True)

        await process_event({"allowed": True, "api_id": "api-1"})
        assert delivered == []

    async def test_deliver_multiple_webhooks(self, monkeypatch):
        from notification.consumer import process_event

        hooks = [
            {"id": "wh_1", "tenant_id": "t1", "url": "https://a.com/hook",
             "events": ["api.call.*"], "secret": ""},
            {"id": "wh_2", "tenant_id": "t2", "url": "https://b.com/hook",
             "events": ["api.call.succeeded"], "secret": ""},
            {"id": "wh_3", "tenant_id": "t3", "url": "https://c.com/hook",
             "events": ["api.call.failed"], "secret": ""},
        ]

        delivered = []

        async def _recording_deliver(url, payload, secret):
            delivered.append(url)
            return True

        self._patch(monkeypatch, hooks, _recording_deliver)

        await process_event({"allowed": True, "api_id": "api-1"})
        assert len(delivered) == 2
        assert "https://a.com/hook" in delivered
        assert "https://b.com/hook" in delivered
        assert "https://c.com/hook" not in delivered

    async def test_retry_on_failure_then_succeeds(self, monkeypatch):
        from notification.consumer import process_event

        hooks = [
            {"id": "wh_1", "tenant_id": "t1", "url": "https://a.com/hook",
             "events": ["api.call.*"], "secret": ""},
        ]

        attempt = [0]
        attempts_log = []

        async def _retry_deliver(url, payload, secret):
            attempt[0] += 1
            attempts_log.append(attempt[0])
            return attempt[0] >= 3  # fail, fail, succeed

        self._patch(monkeypatch, hooks, _retry_deliver)

        await process_event({"allowed": True, "api_id": "api-1"})
        assert len(attempts_log) == 3

    async def test_retry_exhausted(self, monkeypatch):
        from notification.consumer import process_event

        hooks = [
            {"id": "wh_1", "tenant_id": "t1", "url": "https://a.com/hook",
             "events": ["api.call.*"], "secret": ""},
        ]

        attempts = []

        async def _exhausted_deliver(url, payload, secret):
            attempts.append(1)
            return False

        self._patch(monkeypatch, hooks, _exhausted_deliver)

        await process_event({"allowed": True, "api_id": "api-1"})
        assert len(attempts) == 3  # MAX_RETRIES=3, 全部失败放弃

    async def test_allowed_false_sends_failed_event(self, monkeypatch):
        from notification.consumer import process_event

        hooks = [
            {"id": "wh_1", "tenant_id": "t1", "url": "https://a.com/hook",
             "events": ["api.call.failed"], "secret": ""},
        ]

        delivered = []

        async def _recording_deliver(url, payload, secret):
            delivered.append((url, payload["event"], payload["data"]))
            return True

        self._patch(monkeypatch, hooks, _recording_deliver)

        await process_event({"allowed": False, "api_id": "api-1", "app_id": "app-1"})
        assert len(delivered) == 1
        assert delivered[0][1] == "api.call.failed"
        assert delivered[0][2]["allowed"] is False
