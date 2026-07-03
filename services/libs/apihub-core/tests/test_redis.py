"""Redis 客户端测试 —— Key 自动加租户前缀。

用 monkeypatch 替换内部 `_client`，不依赖真实 Redis。
"""

import pytest
from apihub_core import redis as redis_mod
from apihub_core.tenant import set_tenant_context


class _FakeRedis:
    """最小可用 fake —— 记录所有调用以便断言。"""

    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []

    async def get(self, key, *args, **kwargs):
        self.calls.append(("get", (key,) + args, kwargs))
        return None

    async def set(self, key, value, ex=None, **kwargs):
        self.calls.append(("set", (key, value), {"ex": ex, **kwargs}))
        return True

    async def incrby(self, key, amount=1):
        self.calls.append(("incrby", (key, amount), {}))
        return 1

    async def expire(self, key, seconds, *args, **kwargs):
        self.calls.append(("expire", (key, seconds), kwargs))

    async def delete(self, *keys):
        self.calls.append(("delete", keys, {}))
        return 1

    async def aclose(self):
        pass


@pytest.fixture
def fake_redis(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(redis_mod, "_client", fake)
    return fake


class TestTenantPrefix:
    async def test_t_set_with_tenant(self, fake_redis, tenant_a):
        set_tenant_context(tenant_a)
        await redis_mod.t_set("foo", "bar", ex=60)

        assert len(fake_redis.calls) == 1
        op, args, kwargs = fake_redis.calls[0]
        assert op == "set"
        assert args[0] == "t:tenant_a:foo"
        assert args[1] == "bar"
        assert kwargs["ex"] == 60

    async def test_t_get_with_tenant(self, fake_redis, tenant_a):
        set_tenant_context(tenant_a)
        await redis_mod.t_get("foo:bar")

        op, args, _ = fake_redis.calls[0]
        assert op == "get"
        assert args[0] == "t:tenant_a:foo:bar"

    async def test_t_incr_with_tenant(self, fake_redis, tenant_a):
        set_tenant_context(tenant_a)
        await redis_mod.t_incr("rate_limit:calls")

        op, args, _ = fake_redis.calls[0]
        assert op == "incrby"
        assert args[0] == "t:tenant_a:rate_limit:calls"
        assert args[1] == 1

    async def test_t_incr_custom_amount(self, fake_redis, tenant_a):
        set_tenant_context(tenant_a)
        await redis_mod.t_incr("counter", 5)

        _, args, _ = fake_redis.calls[0]
        assert args[1] == 5

    async def test_t_expire_with_tenant(self, fake_redis, tenant_a):
        set_tenant_context(tenant_a)
        await redis_mod.t_expire("lock:abc", 30)

        op, args, _ = fake_redis.calls[0]
        assert op == "expire"
        assert args[0] == "t:tenant_a:lock:abc"
        assert args[1] == 30

    async def test_t_delete_with_tenant(self, fake_redis, tenant_a):
        set_tenant_context(tenant_a)
        await redis_mod.t_delete("temp:key")

        op, args, _ = fake_redis.calls[0]
        assert op == "delete"
        assert "t:tenant_a:temp:key" in args[0]


class TestNoTenantContext:
    async def test_t_set_without_tenant_does_not_prefix(self, fake_redis):
        """没租户上下文时不应加前缀（运维场景 / 启动代码）。"""
        await redis_mod.t_set("global:config", "v1")

        op, args, _ = fake_redis.calls[0]
        assert args[0] == "global:config"  # 不带 t:...:

    async def test_t_get_without_tenant(self, fake_redis):
        await redis_mod.t_get("healthcheck")
        _, args, _ = fake_redis.calls[0]
        assert args[0] == "healthcheck"


class TestRawClient:
    """raw_client 用于跨租户操作（仅超管）—— 不应加前缀。"""

    def test_raw_client_does_not_prefix(self, fake_redis, tenant_a):
        set_tenant_context(tenant_a)
        client = redis_mod.raw_client()
        assert client is fake_redis  # 拿到原始 client，前缀由调用方自己管


class TestInitialization:
    async def test_raises_when_not_initialized(self, monkeypatch):
        monkeypatch.setattr(redis_mod, "_client", None)
        with pytest.raises(RuntimeError, match="Redis not initialized"):
            await redis_mod.t_set("foo", "bar")

    def test_raw_client_raises_when_not_initialized(self, monkeypatch):
        monkeypatch.setattr(redis_mod, "_client", None)
        with pytest.raises(RuntimeError, match="Redis not initialized"):
            redis_mod.raw_client()
