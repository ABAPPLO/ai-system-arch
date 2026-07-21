"""auth repository HMAC secret 生命周期单测（stub PG）。"""

from contextlib import asynccontextmanager

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("HMAC_SECRET_KEY", "a" * 64)
    monkeypatch.setenv("PG_HOST", "localhost")
    monkeypatch.setenv("PG_USER", "apihub")
    monkeypatch.setenv("PG_PASSWORD", "t")
    monkeypatch.setenv("REDIS_HOST", "localhost")
    from apihub_core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_create_api_key_signing_true_returns_secret(monkeypatch):
    from apihub_core import crypto
    from auth import repository

    captured = {}

    class _Conn:
        async def fetchrow(self, sql, *args):
            return {"id": "app_x", "tenant_id": "tenant_a"}

        async def execute(self, sql, *args):
            captured["insert_args"] = args

    @asynccontextmanager
    async def _db_session():
        yield _Conn()

    monkeypatch.setattr(repository.db, "db_session", _db_session)
    rec = await repository.create_api_key(
        key_id="key_1",
        app_id="app_x",
        tenant_id="tenant_a",
        name="n",
        key_hash="h",
        display_prefix="ak_xxxxxxxx",
        scopes=[],
        expires_at=None,
        signing=True,
    )
    assert rec["hmac_secret"] is not None and len(rec["hmac_secret"]) >= 32
    # INSERT 第 9 个 arg ($9) = hmac_secret_encrypted（加密 blob，非明文）
    assert captured["insert_args"][8] != rec["hmac_secret"]
    assert crypto.decrypt_secret(captured["insert_args"][8]) == rec["hmac_secret"]


async def test_create_api_key_signing_false_no_secret(monkeypatch):
    from auth import repository

    class _Conn:
        async def fetchrow(self, sql, *args):
            return {"id": "app_x", "tenant_id": "tenant_a"}

        async def execute(self, sql, *args):
            pass

    @asynccontextmanager
    async def _db_session():
        yield _Conn()

    monkeypatch.setattr(repository.db, "db_session", _db_session)
    rec = await repository.create_api_key(
        key_id="key_1",
        app_id="app_x",
        tenant_id="tenant_a",
        name="n",
        key_hash="h",
        display_prefix="ak_xxxxxxxx",
        scopes=[],
        expires_at=None,
        signing=False,
    )
    assert rec["hmac_secret"] is None
