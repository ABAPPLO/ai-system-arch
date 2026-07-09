"""限流核心 —— 执行 Lua + 失败降级。

设计要点：
  - 错误码不区分 tier 直接抛 RATE_LIMITED；调用方看 tier_blocked 决定要不要
    给用户友好提示（如"今日配额已用完"vs"请求过快"）
  - Redis 故障 → 降级（allow + 告警）。理由：限流挂了不应该把所有请求都挡住，
    反而把业务搞挂；保守策略是放行，靠客户端 retry + 业务后端自我保护兜底
  - 时间槽 = int(time / window) —— 固定窗口（docs/04 推荐，简单且够用）
"""

import time

from apihub_core import redis as redis_mod
from apihub_core.errors import ApiError, ErrorCode
from apihub_core.logging import get_logger

from quota.lua_scripts import CHECK_AND_INCR, READ_USAGE, REFUND
from quota.models import (
    TIER_DAY,
    TIER_MINUTE,
    TIER_SECOND,
    LimitRule,
    QuotaCheckResponse,
    QuotaRules,
    UsagePoint,
    UsageResponse,
)

log = get_logger(__name__)

# tier 名 → 窗口长度
_TIER_DEFS = (
    ("second", TIER_SECOND),
    ("minute", TIER_MINUTE),
    ("day", TIER_DAY),
)


def _slot(window_seconds: int, now: int | None = None) -> int:
    """固定窗口时间槽：int(time / window)。"""
    ts = now if now is not None else int(time.time())
    return ts // window_seconds


def _rate_keys(tenant_id: str, app_id: str, api_id: str) -> list[str]:
    """构造 3 个 tier 的 Redis key。

    docs/04-data-model.md §5.4 —— key 包含 tenant / app / api，让每个维度
    独立计数（不互相挤占），同时 tenant_id 在前打散 slot。
    """
    sec_slot = _slot(TIER_SECOND)
    min_slot = _slot(TIER_MINUTE)
    day_slot = _slot(TIER_DAY)
    prefix = f"t:{tenant_id}:rate:{api_id}:{app_id}"
    return [
        f"{prefix}:s:{sec_slot}",
        f"{prefix}:m:{min_slot}",
        f"{prefix}:d:{day_slot}",
    ]


async def check_and_consume(
    tenant_id: str,
    app_id: str,
    api_id: str,
    rules: QuotaRules,
    cost: int = 1,
) -> QuotaCheckResponse:
    """原子 check + 扣减。

    返回 allowed=True 时表示已成功扣减（Redis 计数 +cost）。
    返回 allowed=False 时表示某层已超（不会扣减当前 cost，但**之前的 tier 可能已扣**）。

    Redis 故障时降级：返回 allowed=True（保守放行）+ rule_source="fallback"。
    """
    maxes, ttls, active_tiers = _compile_rules(rules)

    # 整个 api 完全没配限流 → 直接放行，连 Redis 都不打
    if not active_tiers:
        return QuotaCheckResponse(
            allowed=True,
            rule_source="unlimited",
        )

    keys = _rate_keys(tenant_id, app_id, api_id)
    # 把未启用的 tier 对应位置 max=0（Lua 跳过检查仍 INCR 计数）
    args = list(maxes) + list(ttls) + [cost]

    try:
        client = redis_mod.raw_client()
        result = await client.eval(
            CHECK_AND_INCR,
            len(keys),
            *keys,
            *args,
        )
    except Exception as e:
        log.warning(
            "quota_redis_fallback",
            tenant_id=tenant_id,
            api_id=api_id,
            error=f"{type(e).__name__}: {e}",
        )
        return QuotaCheckResponse(
            allowed=True,
            rule_source="fallback",
            retry_after_seconds=0,
        )

    tier_idx = int(result[0])
    if tier_idx == 0:
        # 全通过
        return QuotaCheckResponse(
            allowed=True,
            remaining=_remaining_for_first_active(rules, active_tiers, cost),
            rule_source="rules",
        )

    # 超了：tier_idx 是绝对位置（1=second, 2=minute, 3=day），跟 KEYS 顺序对齐
    tier_name, _window = _TIER_DEFS[tier_idx - 1]
    # max_count 从原始 rules 取（active_tiers 已经跳过 disabled 的，但 tier_idx
    # 一定指向真正启用的层，因为 disabled 在 Lua 里 max=0 不会触发 block）
    tier_rule = getattr(rules, tier_name)
    tier_max = tier_rule.max_count if tier_rule else 0
    retry_after = int(result[1])
    log.info(
        "quota_blocked",
        tenant_id=tenant_id,
        app_id=app_id,
        api_id=api_id,
        tier=tier_name,
        used=int(result[2]),
        limit=tier_max,
    )
    return QuotaCheckResponse(
        allowed=False,
        tier_blocked=tier_name,
        limit=tier_max,
        retry_after_seconds=retry_after,
        rule_source="rules",
    )


async def refund(
    tenant_id: str,
    app_id: str,
    api_id: str,
    cost: int = 1,
) -> bool:
    """退回扣的 cost（调用失败时 best-effort）。

    失败不抛 —— 退不退都不影响业务流程，只是计数可能略高。
    """
    keys = _rate_keys(tenant_id, app_id, api_id)
    try:
        client = redis_mod.raw_client()
        await client.eval(REFUND, len(keys), *keys, cost)
        return True
    except Exception as e:
        log.warning(
            "quota_refund_failed",
            tenant_id=tenant_id,
            api_id=api_id,
            error=str(e),
        )
        return False


async def get_usage(
    tenant_id: str,
    app_id: str,
    api_id: str,
    rules: QuotaRules,
) -> UsageResponse:
    """查当前用量（不 INCR）—— 给 /usage 端点用。"""
    keys = _rate_keys(tenant_id, app_id, api_id)
    try:
        client = redis_mod.raw_client()
        flat = await client.eval(READ_USAGE, len(keys), *keys)
    except Exception as e:
        log.warning("quota_usage_failed", error=str(e))
        flat = ["0", -1, "0", -1, "0", -1]

    # flat = [used_s, ttl_s, used_m, ttl_m, used_d, ttl_d]
    def tier_used(i: int) -> int:
        return int(flat[i * 2])

    rule_map = {"second": rules.second, "minute": rules.minute, "day": rules.day}
    points = {}
    for i, (name, window) in enumerate(_TIER_DEFS):
        rule = rule_map.get(name)
        points[name] = UsagePoint(
            window_seconds=window,
            used=tier_used(i),
            limit=rule.max_count if rule and rule.enabled else None,
        )

    return UsageResponse(
        tenant_id=tenant_id,
        app_id=app_id,
        api_id=api_id,
        second=points["second"],
        minute=points["minute"],
        day=points["day"],
    )


def _compile_rules(rules: QuotaRules) -> tuple[list[int], list[int], list[tuple[str, LimitRule]]]:
    """把 QuotaRules 拍平成 (maxes, ttls, active_tiers)。

    active_tiers 是 [(name, rule)] 只包含 enabled 的；maxes/ttls 长度始终 3
    （对齐 Lua 的 KEYS），未启用的 tier 用 max=0 表示不限。
    """
    maxes: list[int] = []
    ttls: list[int] = []
    active_tiers: list[tuple[str, LimitRule]] = []

    for name, window in _TIER_DEFS:
        rule = getattr(rules, name)
        if rule and rule.enabled and rule.max_count > 0:
            maxes.append(rule.max_count)
            active_tiers.append((name, rule))
        else:
            maxes.append(0)  # 0 = Lua 跳过检查但仍 INCR
        ttls.append(window)

    return maxes, ttls, active_tiers


def _remaining_for_first_active(
    rules: QuotaRules,
    active_tiers: list[tuple[str, LimitRule]],
    cost: int,
) -> int | None:
    """放行时返回最严格 tier 的 remaining。"""
    if not active_tiers:
        return None
    # 取最小余量（最严格的）
    return min(r.max_count for _, r in active_tiers) - cost


def raise_if_blocked(resp: QuotaCheckResponse) -> None:
    """如果被挡了，抛 ApiError(RATE_LIMITED or TENANT_QUOTA_EXCEEDED)。

    区分：
      - tier_blocked == 'day' → 日配额超 → TENANT_QUOTA_EXCEEDED
      - 其他 → RATE_LIMITED
    """
    if resp.allowed:
        return
    if resp.tier_blocked == "day":
        raise ApiError(
            ErrorCode.TENANT_QUOTA_EXCEEDED,
            f"Daily quota exceeded (limit={resp.limit})",
            details={
                "tier": resp.tier_blocked,
                "limit": resp.limit,
                "retry_after_seconds": resp.retry_after_seconds,
            },
        )
    raise ApiError(
        ErrorCode.RATE_LIMITED,
        f"Rate limited at {resp.tier_blocked} tier (limit={resp.limit})",
        details={
            "tier": resp.tier_blocked,
            "limit": resp.limit,
            "retry_after_seconds": resp.retry_after_seconds,
        },
    )
