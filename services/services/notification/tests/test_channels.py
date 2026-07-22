"""Channel 单测：Email（mock aiosmtplib）/ DingTalk（mock httpx，验签名）。"""

import base64
import hashlib
import hmac
import urllib.parse

# ---------- EmailChannel ----------


class _FakeSMTP:
    def __init__(self, *a, **kw):
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def login(self, user, pwd):
        self.calls.append(("login", user, pwd))

    async def send_message(self, msg):
        self.calls.append(("send_message", msg["From"], msg["To"], msg["Subject"]))


class TestEmailChannel:
    async def test_send_uses_config(self, monkeypatch):
        from notification.channels import email as email_mod

        fake = _FakeSMTP()
        monkeypatch.setattr(email_mod.aiosmtplib, "SMTP", lambda *a, **kw: fake)
        monkeypatch.delenv("NOTIFICATION_SMTP_HOST", raising=False)

        from notification.channels.base import NotificationMessage

        result = await email_mod.EmailChannel().send(
            NotificationMessage(
                recipient="a@b.com",
                subject="S",
                body="B",
                channel_type="email",
                config={
                    "smtp_host": "mail",
                    "smtp_port": "465",
                    "smtp_user": "u",
                    "smtp_password": "p",
                    "from_addr": "from@x.com",
                    "use_tls": True,
                },
            )
        )
        assert result.success is True
        assert fake.calls[0] == ("login", "u", "p")
        assert fake.calls[1][2] == "a@b.com"

    async def test_no_config_no_env_returns_failure(self, monkeypatch):
        from notification.channels import email as email_mod

        monkeypatch.delenv("NOTIFICATION_SMTP_HOST", raising=False)
        from notification.channels.base import NotificationMessage

        result = await email_mod.EmailChannel().send(
            NotificationMessage(
                recipient="a@b.com", subject="S", body="B", channel_type="email", config={}
            )
        )
        assert result.success is False
        assert "no smtp config" in (result.error or "")

    async def test_platform_env_fallback(self, monkeypatch):
        from notification.channels import email as email_mod

        fake = _FakeSMTP()
        monkeypatch.setattr(email_mod.aiosmtplib, "SMTP", lambda *a, **kw: fake)
        monkeypatch.setenv("NOTIFICATION_SMTP_HOST", "platform-mail")
        monkeypatch.setenv("NOTIFICATION_SMTP_FROM_ADDR", "noreply@platform.com")
        from notification.channels.base import NotificationMessage

        result = await email_mod.EmailChannel().send(
            NotificationMessage(
                recipient="a@b.com", subject="S", body="B", channel_type="email", config={}
            )
        )
        assert result.success is True  # tenant config 空→回退平台 env

    async def test_network_error_is_result_not_raise(self, monkeypatch):
        from notification.channels import email as email_mod

        class _Boom:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *e):
                return False

            async def login(self, *a):
                pass

            async def send_message(self, m):
                raise ConnectionRefusedError("boom")

        monkeypatch.setattr(email_mod.aiosmtplib, "SMTP", lambda *a, **kw: _Boom())
        monkeypatch.delenv("NOTIFICATION_SMTP_HOST", raising=False)
        from notification.channels.base import NotificationMessage

        result = await email_mod.EmailChannel().send(
            NotificationMessage(
                recipient="a@b.com",
                subject="S",
                body="B",
                channel_type="email",
                config={"smtp_host": "mail"},
            )
        )
        assert result.success is False
        assert "boom" in (result.error or "")


# ---------- DingTalkChannel ----------


class _FakeResp:
    def __init__(self, json_data):
        self._j = json_data

    def json(self):
        return self._j


class _FakeHttpxClient:
    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *e):
        return False

    async def post(self, url, json=None, timeout=None):  # noqa: ASYNC109 -- mock matches httpx AsyncClient.post signature
        self.url = url
        return _FakeResp({"errcode": 0, "msgid": "mid_123"})


class TestDingTalkChannel:
    async def test_signs_url_and_parses_success(self, monkeypatch):
        from notification.channels import dingtalk as dt_mod

        fake = _FakeHttpxClient()
        monkeypatch.setattr(dt_mod.httpx, "AsyncClient", lambda *a, **kw: fake)

        from notification.channels.base import NotificationMessage

        await dt_mod.DingTalkChannel().send(
            NotificationMessage(
                recipient="",
                subject="S",
                body="B",
                channel_type="dingtalk",
                config={
                    "webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=T",
                    "secret": "SECtest",
                },
            )
        )
        assert "timestamp=" in fake.url and "sign=" in fake.url
        from urllib.parse import parse_qs, urlparse

        q = parse_qs(urlparse(fake.url).query)
        ts = int(q["timestamp"][0])
        # parse_qs URL-decodes values; pull the raw (still percent-encoded) sign
        # so it matches the quote_plus form emitted by dingtalk._sign().
        got = urlparse(fake.url).query.split("sign=")[1]
        string_to_sign = f"{ts}\nSECtest"
        exp = urllib.parse.quote_plus(
            base64.b64encode(hmac.new(b"SECtest", string_to_sign.encode(), hashlib.sha256).digest())
        )
        assert got == exp

    async def test_success_returns_provider_msg_id(self, monkeypatch):
        from notification.channels import dingtalk as dt_mod

        fake = _FakeHttpxClient()
        monkeypatch.setattr(dt_mod.httpx, "AsyncClient", lambda *a, **kw: fake)
        from notification.channels.base import NotificationMessage

        r = await dt_mod.DingTalkChannel().send(
            NotificationMessage(
                recipient="",
                subject="S",
                body="B",
                channel_type="dingtalk",
                config={"webhook_url": "https://x/y?access_token=T", "secret": "s"},
            )
        )
        assert r.success is True and r.provider_msg_id == "mid_123"

    async def test_errcode_nonzero_is_failure(self, monkeypatch):
        from notification.channels import dingtalk as dt_mod

        class _Err(_FakeHttpxClient):
            async def post(self, url, json=None, timeout=None):  # noqa: ASYNC109 -- mock matches httpx AsyncClient.post signature
                self.url = url
                return _FakeResp({"errcode": 310000, "errmsg": "sign not match"})

        monkeypatch.setattr(dt_mod.httpx, "AsyncClient", lambda *a, **kw: _Err())
        from notification.channels.base import NotificationMessage

        r = await dt_mod.DingTalkChannel().send(
            NotificationMessage(
                recipient="",
                subject="S",
                body="B",
                channel_type="dingtalk",
                config={"webhook_url": "https://x/y?access_token=T", "secret": "s"},
            )
        )
        assert r.success is False and "sign not match" in (r.error or "")

    async def test_no_webhook_url_failure(self):
        from notification.channels import dingtalk as dt_mod
        from notification.channels.base import NotificationMessage

        r = await dt_mod.DingTalkChannel().send(
            NotificationMessage(
                recipient="", subject="S", body="B", channel_type="dingtalk", config={}
            )
        )
        assert r.success is False and "webhook_url" in (r.error or "")

    async def test_no_secret_posts_plain(self, monkeypatch):
        from notification.channels import dingtalk as dt_mod

        fake = _FakeHttpxClient()
        monkeypatch.setattr(dt_mod.httpx, "AsyncClient", lambda *a, **kw: fake)
        from notification.channels.base import NotificationMessage

        await dt_mod.DingTalkChannel().send(
            NotificationMessage(
                recipient="",
                subject="S",
                body="B",
                channel_type="dingtalk",
                config={"webhook_url": "https://x/y?access_token=T"},
            )
        )  # 无 secret
        assert "sign=" not in fake.url  # 不签名


# ---------- registry ----------


class TestRegistry:
    async def test_get_known(self):
        from notification.channels import registry

        assert registry.get("email").channel_type == "email"
        assert registry.get("dingtalk").channel_type == "dingtalk"

    async def test_get_unknown_raises(self):
        import pytest
        from apihub_core.errors import ApiError
        from notification.channels import registry

        with pytest.raises(ApiError):
            registry.get("carrier_pigeon")
