"""缓存层单测 —— fakeredis 真跑 SETEX / DEL / json。"""

from tenant import cache


class TestCache:
    async def test_set_and_get(self, fake_redis):
        await cache.set("t1", {"id": "t1", "status": "active"})
        got = await cache.get("t1")
        assert got == {"id": "t1", "status": "active"}

    async def test_get_missing_returns_none(self, fake_redis):
        assert await cache.get("nope") is None

    async def test_invalidate(self, fake_redis):
        await cache.set("t1", {"id": "t1"})
        await cache.invalidate("t1")
        assert await cache.get("t1") is None

    async def test_invalidate_many(self, fake_redis):
        await cache.set("t1", {"id": "t1"})
        await cache.set("t2", {"id": "t2"})
        await cache.set("t3", {"id": "t3"})
        await cache.invalidate_many(["t1", "t2"])
        assert await cache.get("t1") is None
        assert await cache.get("t2") is None
        assert await cache.get("t3") is not None

    async def test_invalidate_many_empty(self, fake_redis):
        # 不应抛
        await cache.invalidate_many([])

    async def test_corrupt_cache_deleted(self, fake_redis):
        """缓存里存了非 JSON 应该自动删除。"""
        await fake_redis.set("t:t1:meta", "not-json{{{")
        got = await cache.get("t1")
        assert got is None
        # 二次读确认已删
        assert await fake_redis.get("t:t1:meta") is None

    async def test_ttl_set(self, fake_redis):
        await cache.set("t1", {"id": "t1"})
        ttl = await fake_redis.ttl("t:t1:meta")
        # 30 分钟 = 1800s，刚写入应该接近 1800
        assert 1700 < ttl <= 1800
