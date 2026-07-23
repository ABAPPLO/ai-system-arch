"""Admin 钉钉 SSO 单测。"""

import pytest
from apihub_core.config import Settings


def test_bootstrap_unionids_parses_csv():
    s = Settings(dingtalk_client_id="x")  # 其余必填走 conftest env
    s.bootstrap_admin_dingtalk_unionids = "uid1, uid2 ,, uid3"
    from auth.identity import _bootstrap_admin_unionids

    assert _bootstrap_admin_unionids(s) == {"uid1", "uid2", "uid3"}


def test_bootstrap_unionids_empty():
    s = Settings(dingtalk_client_id="x")
    s.bootstrap_admin_dingtalk_unionids = ""
    from auth.identity import _bootstrap_admin_unionids

    assert _bootstrap_admin_unionids(s) == set()


class _FakeConn:
    """记录 SQL + 按预设回 fetchrow。"""

    def __init__(self, existing=None):
        self.existing = existing  # dict | None（既有的 user_account 行）
        self.executed = []

    async def fetchrow(self, sql, *args):
        if "FROM user_account WHERE sso_provider" in sql:
            return self.existing
        if "RETURNING" in sql and self.existing:
            return {"id": self.existing["id"]}
        return None

    async def execute(self, sql, *args):
        self.executed.append((sql, args))


class _FakeSession:
    def __init__(self, existing=None):
        self._conn = _FakeConn(existing)

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_upsert_sso_user_creates_new(monkeypatch):
    s = Settings(dingtalk_client_id="x")
    s.bootstrap_admin_dingtalk_unionids = "UID_ADMIN"
    monkeypatch.setattr("apihub_core.config.get_settings", lambda: s)
    fake = _FakeSession(existing=None)
    monkeypatch.setattr("apihub_core.db.admin_db_session", lambda **kw: fake)

    from auth import identity

    result = await identity.upsert_sso_user(union_id="UID_ADMIN", name="Alice")
    assert result["is_platform_admin"] is True
    assert result["name"] == "Alice"
    assert any("INSERT INTO user_account" in sql for sql, _ in fake._conn.executed)


@pytest.mark.asyncio
async def test_upsert_sso_user_relogin_non_admin(monkeypatch):
    s = Settings(dingtalk_client_id="x")
    s.bootstrap_admin_dingtalk_unionids = ""  # 不命中
    monkeypatch.setattr("apihub_core.config.get_settings", lambda: s)
    existing = {"id": "u_existing", "is_platform_admin": False}
    fake = _FakeSession(existing=existing)
    monkeypatch.setattr("apihub_core.db.admin_db_session", lambda **kw: fake)

    from auth import identity

    result = await identity.upsert_sso_user(union_id="UID_X", name="Bob")
    assert result["user_id"] == "u_existing"
    assert result["is_platform_admin"] is False
    assert not any("INSERT INTO user_account" in sql for sql, _ in fake._conn.executed)


def test_build_authorize_url():
    from auth import dingtalk

    url = dingtalk.build_authorize_url(
        client_id="cid",
        redirect_uri="http://localhost:5173/login/callback",
        state="xyz",
    )
    assert url.startswith("https://login.dingtalk.com/oauth2/auth?")
    assert "client_id=cid" in url
    assert "state=xyz" in url
    assert "scope=openid" in url


@pytest.mark.asyncio
async def test_mock_exchange_and_userinfo():
    from auth import dingtalk

    s = Settings(dingtalk_client_id="cid", dingtalk_mock_mode=True)
    token = await dingtalk.exchange_code_for_token(settings=s, code="mock:UID1:Alice")
    assert token == "mock-token:UID1:Alice"
    info = await dingtalk.fetch_userinfo(settings=s, access_token=token)
    assert info == {"union_id": "UID1", "name": "Alice"}
