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
        signing=False,
    )
    assert rec["hmac_secret"] is None
    # column 实际为 NULL（$9），不仅仅是返回值
    assert captured["insert_args"][8] is None


async def test_get_hmac_secret_plaintext_enrolled_active_returns_plaintext(monkeypatch):
    from apihub_core import crypto
    from auth import repository

    plaintext = "plaintext-secret-value-1234567890"
    encrypted = crypto.encrypt_secret(plaintext)

    class _Conn:
        async def fetchrow(self, sql, *args):
            return {"hmac_secret_encrypted": encrypted}

    @asynccontextmanager
    async def _admin_db_session(*, audit_reason=None):
        yield _Conn()

    monkeypatch.setattr(repository.db, "admin_db_session", _admin_db_session)
    got = await repository.get_hmac_secret_plaintext("key_1")
    assert got == plaintext


async def test_get_hmac_secret_plaintext_missing_inactive_null_returns_none(monkeypatch):
    from auth import repository

    @asynccontextmanager
    async def _admin_db_session(*, audit_reason=None):
        class _Conn:
            def __init__(self, row):
                self._row = row

            async def fetchrow(self, sql, *args):
                return self._row

        yield _Conn(None)

    monkeypatch.setattr(repository.db, "admin_db_session", _admin_db_session)
    # 行不存在
    assert await repository.get_hmac_secret_plaintext("missing") is None

    @asynccontextmanager
    async def _admin_db_session_null(*, audit_reason=None):
        class _Conn:
            async def fetchrow(self, sql, *args):
                return {"hmac_secret_encrypted": None}

        yield _Conn()

    monkeypatch.setattr(repository.db, "admin_db_session", _admin_db_session_null)
    # enrolled 列为 NULL
    assert await repository.get_hmac_secret_plaintext("unenrolled") is None


async def test_rotate_hmac_secret_enrolled_active_returns_new_plaintext(monkeypatch):
    from apihub_core import crypto
    from auth import repository

    old_plaintext = "old-secret-rotate-me-0987654321"
    captured = {}

    class _Conn:
        async def fetchrow(self, sql, *args):
            captured["update_args"] = args
            return {"id": "key_1", "key_hash": "keyhash_abc"}

    @asynccontextmanager
    async def _admin_db_session(*, audit_reason=None):
        yield _Conn()

    monkeypatch.setattr(repository.db, "admin_db_session", _admin_db_session)
    result = await repository.rotate_hmac_secret("key_1", "tenant_a")

    assert result["key_id"] == "key_1"
    assert result["key_hash"] == "keyhash_abc"
    new_secret = result["hmac_secret"]
    assert isinstance(new_secret, str) and len(new_secret) >= 32
    # 新明文 ≠ 已知旧值
    assert new_secret != old_plaintext
    # UPDATE args = (key_id, tenant_id, encrypted_blob)
    update_args = captured["update_args"]
    assert update_args[0] == "key_1"
    assert update_args[1] == "tenant_a"  # C1: tenant_id 进 WHERE 过滤防跨租户 rotate 劫持
    encrypted_blob = update_args[2]
    assert encrypted_blob != new_secret
    assert crypto.decrypt_secret(encrypted_blob) == new_secret


async def test_rotate_hmac_secret_non_enrolled_or_inactive_raises_not_found(monkeypatch):
    import pytest as _pytest
    from apihub_core.errors import ApiError, ErrorCode
    from auth import repository

    @asynccontextmanager
    async def _admin_db_session(*, audit_reason=None):
        class _Conn:
            async def fetchrow(self, sql, *args):
                # UPDATE ... RETURNING 未命中行 → None
                return None

        yield _Conn()

    monkeypatch.setattr(repository.db, "admin_db_session", _admin_db_session)
    with _pytest.raises(ApiError) as exc:
        await repository.rotate_hmac_secret("key_missing", "tenant_a")
    assert exc.value.code == ErrorCode.NOT_FOUND
