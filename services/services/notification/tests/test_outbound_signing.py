"""R2e T8: outbound webhook 签名单测。

覆盖：
  - _deliver 带 secret → X-Webhook-Signature: hmac-sha256=<hex>，与 sign_webhook 逐字节兼容
  - _deliver 空 secret → 不带签名头（向后兼容）
  - create_webhook secret=None → 平台生成明文一次 + DB 存加密
  - create_webhook client-supplied secret → 加密存同值
"""

from contextlib import asynccontextmanager

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("HMAC_SECRET_KEY", "a" * 64)
    from apihub_core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_deliver_sets_webhook_signature_header(monkeypatch):
    import apihub_core.signing as signing
    from notification import consumer

    captured = {}

    class _Resp:
        status_code = 200

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, url, content=None, headers=None):
            captured["body"] = content
            captured["sig"] = headers.get("X-Webhook-Signature", "")
            return _Resp()

    monkeypatch.setattr(consumer.httpx, "AsyncClient", lambda *a, **kw: _Client())
    ok = await consumer._deliver("http://x", {"e": 1}, "wh_secret")
    assert ok is True
    assert captured["sig"].startswith("hmac-sha256=")  # 头格式（R2e 改）
    sig_hex = captured["sig"].removeprefix("hmac-sha256=")
    assert signing.verify_webhook("wh_secret", captured["body"], sig_hex) is True


async def test_deliver_no_secret_no_signature(monkeypatch):
    from notification import consumer

    captured = {}

    class _Resp:
        status_code = 200

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, url, content=None, headers=None):
            captured["sig"] = headers.get("X-Webhook-Signature", "")
            return _Resp()

    monkeypatch.setattr(consumer.httpx, "AsyncClient", lambda *a, **kw: _Client())
    await consumer._deliver("http://x", {"e": 1}, "")
    assert captured["sig"] == ""


async def test_create_webhook_generates_secret(monkeypatch):
    from apihub_core import crypto
    from notification import repository

    captured = {}

    class _Conn:
        async def execute(self, sql, *args):
            captured["args"] = args

    @asynccontextmanager
    async def _db():
        yield _Conn()

    monkeypatch.setattr(repository.db, "db_session", _db)
    result = await repository.create_webhook(
        tenant_id="t1", url="http://x", events=["api.call.*"], secret=None
    )
    assert result["hmac_secret"] is not None
    assert len(result["hmac_secret"]) >= 32
    # INSERT 第 5 个 arg（index 4）= secret_encrypted（加密 blob，非明文）
    assert captured["args"][4] != result["hmac_secret"]
    assert crypto.decrypt_secret(captured["args"][4]) == result["hmac_secret"]


async def test_create_webhook_client_supplied_secret_compatible(monkeypatch):
    from apihub_core import crypto
    from notification import repository

    captured = {}

    class _Conn:
        async def execute(self, sql, *args):
            captured["args"] = args

    @asynccontextmanager
    async def _db():
        yield _Conn()

    monkeypatch.setattr(repository.db, "db_session", _db)
    result = await repository.create_webhook(
        tenant_id="t1", url="http://x", events=["api.call.*"], secret="my_secret"
    )
    assert result["hmac_secret"] == "my_secret"
    assert crypto.decrypt_secret(captured["args"][4]) == "my_secret"
