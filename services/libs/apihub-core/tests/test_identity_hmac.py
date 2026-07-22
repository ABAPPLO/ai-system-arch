"""identity 缓存 hmac_enrolled + secret 缓存单测。"""

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


@pytest.fixture
def fake_redis(monkeypatch):
    import fakeredis.aioredis
    from apihub_core import redis as redis_mod

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_mod, "_client", fake)
    return fake


async def test_write_read_hmac_secret_roundtrip(fake_redis):
    from apihub_core import crypto, identity

    enc = crypto.encrypt_secret("plaintext_secret")
    await identity.write_hmac_secret("ak_xxx", enc, ttl=300)
    got = await identity.read_hmac_secret("ak_xxx")
    assert got == enc  # 返回加密 blob，不 decrypt
    assert crypto.decrypt_secret(got) == "plaintext_secret"


async def test_read_hmac_secret_miss(fake_redis):
    from apihub_core import identity

    assert await identity.read_hmac_secret("ak_missing") is None


async def test_delete_hmac_secret(fake_redis):
    from apihub_core import crypto, identity

    await identity.write_hmac_secret("ak_xxx", crypto.encrypt_secret("s"), ttl=300)
    await identity.delete_hmac_secret("ak_xxx")
    assert await identity.read_hmac_secret("ak_xxx") is None


async def test_delete_secret_does_not_clear_identity(fake_redis):
    """rotate 只清 secret 缓存，不清 identity。"""
    from apihub_core import identity

    await identity.write_identity("ak_xxx", {"tenant_id": "t1", "hmac_enrolled": True}, ttl=300)
    await identity.write_hmac_secret("ak_xxx", "encblob", ttl=300)
    await identity.delete_hmac_secret("ak_xxx")
    idc = await identity.read_identity("ak_xxx")
    assert idc is not None and idc["tenant_id"] == "t1"  # identity 仍在
