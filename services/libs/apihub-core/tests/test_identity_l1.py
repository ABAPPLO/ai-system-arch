"""identity opt-in L1 + pipeline 读单测。"""
import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("PG_HOST", "localhost")
    monkeypatch.setenv("PG_USER", "apihub")
    monkeypatch.setenv("PG_PASSWORD", "t")
    monkeypatch.setenv("REDIS_HOST", "localhost")
    from apihub_core.config import get_settings
    get_settings.cache_clear()
    yield
    from apihub_core import identity
    identity.configure_l1(identity=None, secret=None)  # 清 L1（默认关）
    get_settings.cache_clear()


@pytest.fixture
def fake_redis(monkeypatch):
    import fakeredis.aioredis
    from apihub_core import redis as redis_mod
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_mod, "_client", fake)
    return fake


async def test_read_identity_l1_hit_skips_redis(fake_redis):
    from apihub_core import identity
    from apihub_core.l1 import TTLCache
    identity.configure_l1(identity=TTLCache(ttl=5), secret=TTLCache(ttl=5))
    identity._identity_l1.set("ak_x", {"is_active": True, "tenant_id": "t1"})
    got = await identity.read_identity("ak_x")
    assert got == {"is_active": True, "tenant_id": "t1"}
    from apihub_core.redis import raw_client
    assert await raw_client().get(identity.identity_cache_key("ak_x")) is None


async def test_read_identity_l1_miss_falls_to_redis_and_backfills(fake_redis):
    from apihub_core import identity
    from apihub_core.l1 import TTLCache
    identity.configure_l1(identity=TTLCache(ttl=5), secret=TTLCache(ttl=5))
    await identity.write_identity("ak_x", {"is_active": True, "tenant_id": "t1"}, ttl=300)
    identity._identity_l1.clear()
    got = await identity.read_identity("ak_x")
    assert got["tenant_id"] == "t1"
    assert identity._identity_l1.get("ak_x") is not None


async def test_read_identity_unconfigured_no_l1(fake_redis):
    from apihub_core import identity
    await identity.write_identity("ak_x", {"is_active": True, "tenant_id": "t1"}, ttl=300)
    got = await identity.read_identity("ak_x")
    assert got["tenant_id"] == "t1"
    assert identity._identity_l1 is None


async def test_delete_identity_invalidates_l1(fake_redis):
    from apihub_core import identity
    from apihub_core.l1 import TTLCache
    identity.configure_l1(identity=TTLCache(ttl=5), secret=TTLCache(ttl=5))
    identity._identity_l1.set("ak_x", {"is_active": True, "tenant_id": "t1"})
    await identity.delete_identity("ak_x")
    assert identity._identity_l1.get("ak_x") is None


async def test_read_identity_and_hmac_secret_both_l1_hit(fake_redis):
    from apihub_core import identity
    from apihub_core.l1 import TTLCache
    identity.configure_l1(identity=TTLCache(ttl=5), secret=TTLCache(ttl=5))
    identity._identity_l1.set("ak_x", {"is_active": True, "tenant_id": "t1"})
    identity._secret_l1.set("ak_x", "encblob")
    ident, sec = await identity.read_identity_and_hmac_secret("ak_x")
    assert ident["tenant_id"] == "t1"
    assert sec == "encblob"


async def test_read_identity_and_hmac_secret_redis_pipeline(fake_redis):
    """两 L1 miss → 一次 pipeline 取两 Redis key（unenrolled：secret None）。"""
    from apihub_core import identity
    from apihub_core.l1 import TTLCache
    identity.configure_l1(identity=TTLCache(ttl=5), secret=TTLCache(ttl=5))
    await identity.write_identity("ak_x", {"is_active": True, "tenant_id": "t1"}, ttl=300)
    ident, sec = await identity.read_identity_and_hmac_secret("ak_x")
    assert ident is not None and ident["tenant_id"] == "t1"
    assert sec is None  # 未 enrolled
    assert identity._identity_l1.get("ak_x") is not None  # 回填
