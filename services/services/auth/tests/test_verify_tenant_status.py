"""verify_api_key_record 的租户状态检查 —— suspend/close 必须拒 Key。

tenant-svc 的 suspend/close 一直宣称"auth 会拒绝"，但 verify SQL JOIN 了
tenant 却从不看 status；本文件锁死该行为（20002 TENANT_DISABLED 的落地）。
"""

import contextlib
from datetime import UTC, datetime, timedelta

import pytest


class _FakeConn:
    def __init__(self, row):
        self._row = row

    async def fetchrow(self, sql, *args):
        return self._row

    async def execute(self, *a):
        return "OK"


def _patch_db(monkeypatch, row):
    import auth.repository as repo
    from apihub_core import db

    @contextlib.asynccontextmanager
    async def _fake_session(**kw):
        yield _FakeConn(row)

    monkeypatch.setattr(db, "admin_db_session", _fake_session)
    monkeypatch.setattr(repo.db, "admin_db_session", _fake_session)


def _row(tenant_status: str, key_status: str = "active") -> dict:
    return {
        "id": "key_1",
        "app_id": "app_1",
        "scopes": ["read"],
        "status": key_status,
        "expires_at": datetime.now(UTC) + timedelta(days=1),
        "tenant_id": "t1",
        "hmac_secret_encrypted": None,
        "tenant_type": "internal",
        "tenant_status": tenant_status,
        "platform_admin_flag": None,
    }


class TestVerifyTenantStatus:
    @pytest.mark.asyncio
    async def test_active_tenant_ok(self, monkeypatch):
        from auth.repository import verify_api_key_record

        _patch_db(monkeypatch, _row("active"))
        rec = await verify_api_key_record("ak_x")
        assert rec is not None
        assert rec["tenant_id"] == "t1"
        assert rec["is_active"] is True

    @pytest.mark.asyncio
    async def test_suspended_tenant_rejected(self, monkeypatch):
        """suspend 的租户：Key 一律拒绝（返回 None → 401）。"""
        from auth.repository import verify_api_key_record

        _patch_db(monkeypatch, _row("suspended"))
        assert await verify_api_key_record("ak_x") is None

    @pytest.mark.asyncio
    async def test_closed_tenant_rejected(self, monkeypatch):
        from auth.repository import verify_api_key_record

        _patch_db(monkeypatch, _row("closed"))
        assert await verify_api_key_record("ak_x") is None

    @pytest.mark.asyncio
    async def test_revoked_key_rejected(self, monkeypatch):
        from auth.repository import verify_api_key_record

        _patch_db(monkeypatch, _row("active", key_status="revoked"))
        assert await verify_api_key_record("ak_x") is None
