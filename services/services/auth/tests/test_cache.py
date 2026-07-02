"""Redis 缓存层测试 —— mock raw_client。"""

import json

import pytest
from apihub_core import redis as redis_mod
from auth import cache as cache_mod
from auth.apikey import cache_key, generate_api_key, hash_api_key


class _FakeRedis:
    """最小 fake —— 用 dict 模拟 Redis。"""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.calls: list[tuple[str, tuple]] = []

    async def setex(self, key: str, ttl: int, value: str):
        self.calls.append(("setex", (key, ttl, value)))
        self.store[key] = value
        return True

    async def get(self, key: str):
        self.calls.append(("get", (key,)))
        return self.store.get(key)

    async def delete(self, key: str):
        self.calls.append(("delete", (key,)))
        return self.store.pop(key, None)


@pytest.fixture
def fake_redis(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(redis_mod, "_client", fake)
    return fake


class TestCachePositive:
    async def test_set_with_correct_key(self, fake_redis):
        plaintext, _, _ = generate_api_key()
        data = {"is_active": True, "tenant_id": "t1", "app_id": "a1"}

        await cache_mod.cache_positive(plaintext, data)

        expected_key = cache_key(plaintext)
        assert expected_key in fake_redis.store
        assert json.loads(fake_redis.store[expected_key]) == data

    async def test_get_returns_data(self, fake_redis):
        plaintext, _, _ = generate_api_key()
        data = {"is_active": True, "tenant_id": "t1"}

        await cache_mod.cache_positive(plaintext, data)
        result = await cache_mod.get_cached(plaintext)

        assert result == data

    async def test_get_returns_none_when_missing(self, fake_redis):
        plaintext, _, _ = generate_api_key()
        result = await cache_mod.get_cached(plaintext)
        assert result is None


class TestCacheNegative:
    async def test_negative_marked_invalid(self, fake_redis):
        plaintext, _, _ = generate_api_key()
        await cache_mod.cache_negative(plaintext)

        result = await cache_mod.get_cached(plaintext)
        assert result == {"invalid": True}

    async def test_negative_distinguishable_from_positive(self, fake_redis):
        plaintext, _, _ = generate_api_key()
        await cache_mod.cache_negative(plaintext)
        cached = await cache_mod.get_cached(plaintext)
        assert cached is not None
        assert cached.get("invalid") is True


class TestInvalidate:
    async def test_delete_existing(self, fake_redis):
        plaintext, _, _ = generate_api_key()
        await cache_mod.cache_positive(plaintext, {"x": 1})

        await cache_mod.invalidate(plaintext)

        result = await cache_mod.get_cached(plaintext)
        assert result is None

    async def test_accepts_hash_directly(self, fake_redis):
        """吊销时传 hash 而非明文。"""
        plaintext, _, _ = generate_api_key()
        h = hash_api_key(plaintext)
        await cache_mod.cache_positive(plaintext, {"x": 1})

        await cache_mod.invalidate(h)

        result = await cache_mod.get_cached(plaintext)
        assert result is None

    async def test_delete_missing_is_noop(self, fake_redis):
        """删不存在的 key 不应报错。"""
        await cache_mod.invalidate("ak_nonexistent_123456789012345")


class TestCorruption:
    async def test_corrupt_cache_gets_deleted(self, fake_redis):
        """缓存值损坏（非 JSON）→ get 应删除并返回 None。"""
        plaintext, _, _ = generate_api_key()
        key = cache_key(plaintext)
        fake_redis.store[key] = "not json {{{"

        result = await cache_mod.get_cached(plaintext)
        assert result is None
        # 已被清理
        assert key not in fake_redis.store
