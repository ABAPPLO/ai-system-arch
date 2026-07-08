"""delay_queue 测试 —— 用 fake Redis client 验证 ZSet / Set 操作。"""

import pytest


class _FakeRedis:
    """模拟 redis.asyncio.Redis —— 实现 eval / zadd / srem / sismember / scan_iter / zcount。"""

    def __init__(self):
        self.zsets: dict[str, dict[str, float]] = {}
        self.sets: dict[str, set[str]] = {}

    async def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)

    async def srem(self, key, *members):
        s = self.sets.get(key, set())
        removed = 0
        for m in members:
            if m in s:
                s.discard(m)
                removed += 1
        return removed

    async def sismember(self, key, member):
        return member in self.sets.get(key, set())

    async def zcount(self, key, lo, hi):
        z = self.zsets.get(key, {})
        if hi in ("+inf", "-inf") and lo == "-inf":
            return len(z)
        count = 0
        for v in z.values():
            if lo == "-inf":
                if hi == "+inf" or v <= float(hi):
                    count += 1
            elif hi == "+inf":
                if v >= float(lo):
                    count += 1
            elif float(lo) <= v <= float(hi):
                count += 1
        return count

    async def eval(self, script, numkeys, *args):
        # 解析 LUA 脚本：ZRANGEBYSCORE + ZREM + SADD
        delayed_key = args[0]
        processing_key = args[1]
        now = float(args[2])
        max_count = int(args[3])

        z = self.zsets.get(delayed_key, {})
        due = sorted(
            [m for m, s in z.items() if s <= now],
            key=lambda x: z[x],
        )[:max_count]

        for d in due:
            del z[d]
            self.sets.setdefault(processing_key, set()).add(d)

        return due

    async def scan_iter(self, *, match=None, count=None):  # noqa: ARG002
        for key in list(self.zsets.keys()):
            if match and "*" in match and "retry:delayed" in key:
                yield key


@pytest.fixture
def fake_redis(monkeypatch):
    fake = _FakeRedis()

    from apihub_core import redis as redis_mod

    # raw_client 返回我们的 fake
    monkeypatch.setattr(redis_mod, "raw_client", lambda: fake)
    # 同时替换模块全局 _client（其他操作可能直接用）
    monkeypatch.setattr(redis_mod, "_client", fake)
    return fake


class TestScheduleAndPop:
    async def test_schedule_pushes_to_zset(self, fake_redis):
        from retry_svc import delay_queue

        await delay_queue.schedule(
            tenant_id=42, retry_task_id=100, next_attempt_at_ts=9999.0
        )
        z = fake_redis.zsets["t:42:retry:delayed"]
        assert z["100"] == 9999.0

    async def test_pop_due_returns_and_moves_to_processing(self, fake_redis):
        from retry_svc import delay_queue

        # 推 3 个：1 个到期，2 个没到期
        await delay_queue.schedule(tenant_id=42, retry_task_id=1, next_attempt_at_ts=100.0)
        await delay_queue.schedule(tenant_id=42, retry_task_id=2, next_attempt_at_ts=100.0)
        await delay_queue.schedule(tenant_id=42, retry_task_id=3, next_attempt_at_ts=9999.0)

        due = await delay_queue.pop_due(tenant_id=42, max_count=10, now_ts=200.0)
        assert sorted(due) == [1, 2]

        # processing set 应包含 1, 2
        assert fake_redis.sets["t:42:retry:processing"] == {"1", "2"}
        # delayed 还剩 3
        assert "3" in fake_redis.zsets["t:42:retry:delayed"]

    async def test_pop_respects_max_count(self, fake_redis):
        from retry_svc import delay_queue

        for i in range(5):
            await delay_queue.schedule(
                tenant_id=42, retry_task_id=i, next_attempt_at_ts=100.0
            )
        due = await delay_queue.pop_due(tenant_id=42, max_count=2, now_ts=200.0)
        assert len(due) == 2

    async def test_pop_empty(self, fake_redis):
        from retry_svc import delay_queue

        due = await delay_queue.pop_due(tenant_id=99, max_count=10, now_ts=0)
        assert due == []


class TestCompleteAndProcessing:
    async def test_complete_removes_from_processing(self, fake_redis):
        from retry_svc import delay_queue

        await delay_queue.schedule(tenant_id=42, retry_task_id=7, next_attempt_at_ts=100.0)
        await delay_queue.pop_due(tenant_id=42, max_count=10, now_ts=200.0)
        assert "7" in fake_redis.sets["t:42:retry:processing"]

        await delay_queue.complete(tenant_id=42, retry_task_id=7)
        assert "7" not in fake_redis.sets["t:42:retry:processing"]

    async def test_is_processing(self, fake_redis):
        from retry_svc import delay_queue

        await delay_queue.schedule(tenant_id=42, retry_task_id=7, next_attempt_at_ts=100.0)
        await delay_queue.pop_due(tenant_id=42, max_count=10, now_ts=200.0)

        assert await delay_queue.is_processing(tenant_id=42, retry_task_id=7)
        assert not await delay_queue.is_processing(tenant_id=42, retry_task_id=999)


class TestScanTenants:
    async def test_list_tenants_with_pending(self, fake_redis):
        from retry_svc import delay_queue

        await delay_queue.schedule(tenant_id="tenant_1", retry_task_id=10, next_attempt_at_ts=100.0)
        await delay_queue.schedule(tenant_id="tenant_2", retry_task_id=20, next_attempt_at_ts=100.0)
        await delay_queue.schedule(tenant_id="tenant_3", retry_task_id=30, next_attempt_at_ts=100.0)

        tenants = await delay_queue.list_tenants_with_pending()
        assert tenants == ["tenant_1", "tenant_2", "tenant_3"]

    async def test_get_due_count(self, fake_redis):
        from retry_svc import delay_queue

        await delay_queue.schedule(tenant_id=1, retry_task_id=1, next_attempt_at_ts=100.0)
        await delay_queue.schedule(tenant_id=1, retry_task_id=2, next_attempt_at_ts=100.0)
        await delay_queue.schedule(tenant_id=1, retry_task_id=3, next_attempt_at_ts=9999.0)

        n = await delay_queue.get_due_count(tenant_id=1, now_ts=200.0)
        assert n == 2
