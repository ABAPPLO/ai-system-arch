"""limiter 测试 —— 用 fakeredis 真跑 Lua 脚本。

覆盖：
  - 全通过：3 tier 都没超
  - second tier 超
  - minute tier 超
  - day tier 超 → TENANT_QUOTA_EXCEEDED 语义
  - max=0 = 不限流
  - cost > 1（AI token 折算场景）
  - refund 退回
  - get_usage 不 INCR
  - Redis 故障 → fallback allow
  - 时间槽（slot）切换：跨窗口自动重置
"""

from quota.limiter import (
    _slot,
    check_and_consume,
    get_usage,
    refund,
)
from quota.models import LimitRule, QuotaRules


def rules(second=None, minute=None, day=None) -> QuotaRules:
    return QuotaRules(second=second, minute=minute, day=day)


class TestPass:
    async def test_no_rules_unlimited(self, fake_redis):
        """没配任何限流 → 直接放行，不打 Redis。"""
        resp = await check_and_consume(
            "t1",
            "app1",
            "api1",
            rules=QuotaRules(),
            cost=1,
        )
        assert resp.allowed is True
        assert resp.rule_source == "unlimited"

    async def test_under_limit_passes(self, fake_redis):
        resp = await check_and_consume(
            "t1",
            "app1",
            "api1",
            rules=rules(
                second=LimitRule(window_seconds=1, max_count=10),
                minute=LimitRule(window_seconds=60, max_count=100),
            ),
            cost=1,
        )
        assert resp.allowed is True
        assert resp.tier_blocked is None

    async def test_cost_greater_than_one(self, fake_redis):
        """cost=5 一次扣 5。"""
        await check_and_consume(
            "t1",
            "app1",
            "api1",
            rules=rules(second=LimitRule(window_seconds=1, max_count=100)),
            cost=5,
        )
        # 查 usage 应该显示 used=5
        usage = await get_usage(
            "t1",
            "app1",
            "api1",
            rules=rules(second=LimitRule(window_seconds=1, max_count=100)),
        )
        assert usage.second.used == 5


class TestBlocked:
    async def test_second_tier_blocked(self, fake_redis):
        """秒级限流：max=2，连发 3 次 → 第 3 次挡。"""
        r = rules(second=LimitRule(window_seconds=1, max_count=2))

        r1 = await check_and_consume("t1", "app1", "api1", r)
        r2 = await check_and_consume("t1", "app1", "api1", r)
        r3 = await check_and_consume("t1", "app1", "api1", r)

        assert r1.allowed and r2.allowed
        assert r3.allowed is False
        assert r3.tier_blocked == "second"
        assert r3.limit == 2
        assert r3.retry_after_seconds >= 0

    async def test_minute_tier_blocked(self, fake_redis):
        """分钟级限流：max=2，连发 3 次只挡在 minute（second 设得很宽）。"""
        r = rules(
            second=LimitRule(window_seconds=1, max_count=1000),
            minute=LimitRule(window_seconds=60, max_count=2),
        )

        for _ in range(2):
            assert (await check_and_consume("t1", "app1", "api1", r)).allowed

        blocked = await check_and_consume("t1", "app1", "api1", r)
        assert blocked.allowed is False
        assert blocked.tier_blocked == "minute"

    async def test_day_tier_blocked(self, fake_redis):
        """日级限流：超了 → tier_blocked='day'，给 TENANT_QUOTA_EXCEEDED 判断用。"""
        r = rules(day=LimitRule(window_seconds=86400, max_count=1))

        await check_and_consume("t1", "app1", "api1", r)
        blocked = await check_and_consume("t1", "app1", "api1", r)

        assert blocked.allowed is False
        assert blocked.tier_blocked == "day"
        assert blocked.limit == 1

    async def test_disabled_tier_ignored(self, fake_redis):
        """enabled=False 等同没配。"""
        r = rules(
            second=LimitRule(window_seconds=1, max_count=0, enabled=False),
            minute=LimitRule(window_seconds=60, max_count=100),
        )
        # second 关掉了，连发 50 次都不挡（minute 100）
        for _ in range(50):
            resp = await check_and_consume("t1", "app1", "api1", r)
            assert resp.allowed


class TestRefund:
    async def test_refund_decrements_counter(self, fake_redis):
        """扣 5 → 退 3 → 当前 used 应该是 2。"""
        r = rules(second=LimitRule(window_seconds=1, max_count=100))

        await check_and_consume("t1", "app1", "api1", r, cost=5)
        ok = await refund("t1", "app1", "api1", cost=3)
        assert ok is True

        usage = await get_usage("t1", "app1", "api1", r)
        assert usage.second.used == 2

    async def test_refund_not_below_zero(self, fake_redis):
        """退超了不应该变负数。"""
        r = rules(second=LimitRule(window_seconds=1, max_count=100))
        await check_and_consume("t1", "app1", "api1", r, cost=2)
        await refund("t1", "app1", "api1", cost=10)  # 退比扣的多

        usage = await get_usage("t1", "app1", "api1", r)
        assert usage.second.used == 0


class TestUsage:
    async def test_get_usage_does_not_increment(self, fake_redis):
        """查 usage 不应 INCR。"""
        r = rules(second=LimitRule(window_seconds=1, max_count=100))

        await check_and_consume("t1", "app1", "api1", r, cost=1)
        before = await get_usage("t1", "app1", "api1", r)
        after = await get_usage("t1", "app1", "api1", r)

        assert before.second.used == 1
        assert after.second.used == 1  # 没多打

    async def test_usage_limit_reflects_rules(self, fake_redis):
        """usage.limit 应反映启用的 rule。"""
        r = rules(
            second=LimitRule(window_seconds=1, max_count=10),
            minute=LimitRule(window_seconds=60, max_count=100),
            day=None,  # day 不限
        )
        usage = await get_usage("t1", "app1", "api1", r)
        assert usage.second.limit == 10
        assert usage.minute.limit == 100
        assert usage.day.limit is None  # 没配 → None


class TestFallback:
    async def test_redis_failure_allows(self, monkeypatch):
        """Redis 故障 → 返回 allowed=True + rule_source=fallback（保守放行）。"""
        from apihub_core import redis as redis_mod
        from quota import limiter

        # 模拟 raw_client().eval 抛异常
        class _Boom:
            async def eval(self, *args, **kwargs):
                raise RuntimeError("redis down")

        monkeypatch.setattr(redis_mod, "_client", _Boom())

        r = rules(second=LimitRule(window_seconds=1, max_count=1))
        resp = await limiter.check_and_consume("t1", "app1", "api1", r)

        assert resp.allowed is True
        assert resp.rule_source == "fallback"


class TestSlotIsolation:
    def test_slot_division(self):
        """时间槽计算正确。"""
        # 60 秒窗口
        assert _slot(60, now=0) == 0
        assert _slot(60, now=59) == 0
        assert _slot(60, now=60) == 1
        assert _slot(60, now=119) == 1
        assert _slot(60, now=120) == 2

    def test_different_dimensions_isolated(self, fake_redis):
        """同租户不同 app/api 的计数互不影响。"""
        import asyncio

        async def _go():
            r = rules(second=LimitRule(window_seconds=1, max_count=2))
            # app1 打 2 次（达上限）
            await check_and_consume("t1", "app1", "api1", r)
            await check_and_consume("t1", "app1", "api1", r)
            # app2 应该还能打
            resp = await check_and_consume("t1", "app2", "api1", r)
            assert resp.allowed

        asyncio.run(_go())
