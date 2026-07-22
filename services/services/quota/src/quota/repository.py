"""PG 读规则 —— app / tenant / api_version 三层的 rate_limit JSONB。

合并优先级（高 → 低）：
  1. app.rate_limit        —— 应用层覆盖（某个客户付费版本单独放宽）
  2. tenant.rate_limit     —— 租户层默认
  3. api_version.rate_limit —— 接口层默认（防止没人显式配时空载）
  4. built-in default      —— 全 0（不限流）

每层都是 `{second: {max, window}, minute: {...}, day: {...}}` 格式。
"""

import json
from typing import Any

from apihub_core import db

from quota.models import TIER_DAY, TIER_MINUTE, TIER_SECOND, LimitRule, QuotaRules

# 当所有层都没配时的兜底：完全不限流
EMPTY_RULES = QuotaRules()


def _parse_tier(raw: Any, default_window: int) -> LimitRule | None:
    """单 tier 解析。raw 形如 {'max_count': 100, 'window_seconds': 60, 'enabled': true}。

    兼容简写 {'count': 100, 'window_seconds': 60} 和 {'max': 100}。
    """
    if not raw:
        return None
    if isinstance(raw, int | float):
        # 简写：直接给 max_count，window 用默认
        return LimitRule(window_seconds=default_window, max_count=int(raw))

    if not isinstance(raw, dict):
        return None

    max_count = raw.get("max_count") or raw.get("max") or raw.get("count")
    if max_count is None:
        return None

    window = raw.get("window_seconds") or default_window
    return LimitRule(
        window_seconds=int(window),
        max_count=int(max_count),
        enabled=raw.get("enabled", True),
    )


def _parse_rules_blob(blob: Any) -> QuotaRules:
    """把 PG JSONB 解析成 QuotaRules。"""
    if isinstance(blob, str):
        try:
            blob = json.loads(blob)
        except json.JSONDecodeError:
            return EMPTY_RULES
    if not isinstance(blob, dict):
        return EMPTY_RULES

    return QuotaRules(
        second=_parse_tier(blob.get("second"), TIER_SECOND),
        minute=_parse_tier(blob.get("minute"), TIER_MINUTE),
        day=_parse_tier(blob.get("day"), TIER_DAY),
    )


def _merge(base: QuotaRules, override: QuotaRules) -> QuotaRules:
    """override 优先。每 tier 独立合并。"""
    return QuotaRules(
        second=override.second or base.second,
        minute=override.minute or base.minute,
        day=override.day or base.day,
    )


async def load_rules(tenant_id: str, app_id: str, api_id: str) -> tuple[QuotaRules, str]:
    """按优先级合并 app > tenant > api_version。

    Returns: (merged_rules, source) — source 标记最高优先级来源。
    """
    async with db.admin_db_session() as conn:
        rows = await conn.fetch(
            """
            SELECT
                (SELECT rate_limit FROM app WHERE id = $1 AND tenant_id = $2) AS app_rl,
                (SELECT rate_limit FROM tenant WHERE id = $2)               AS tenant_rl,
                (SELECT rate_limit FROM api_version
                 WHERE api_id = $3 AND tenant_id = $2
                 ORDER BY status = 'published' DESC, created_at DESC LIMIT 1) AS api_rl
            """,
            app_id,
            tenant_id,
            api_id,
        )

    if not rows:
        return EMPTY_RULES, "default"

    row = rows[0]
    api_rules = _parse_rules_blob(row["api_rl"])
    tenant_rules = _parse_rules_blob(row["tenant_rl"])
    app_rules = _parse_rules_blob(row["app_rl"])

    merged = _merge(_merge(api_rules, tenant_rules), app_rules)

    if row["app_rl"]:
        source = "app"
    elif row["tenant_rl"]:
        source = "tenant"
    elif row["api_rl"]:
        source = "api_version"
    else:
        source = "default"

    return merged, source


# ========== Phase 3 计费 ==========

from apihub_core.clickhouse import query_all  # noqa: E402  分段 import（Phase 3 计费段）

from quota.models import PlanSummary  # noqa: E402  分段 import（Phase 3 计费段）


async def get_plan(plan_code: str) -> PlanSummary | None:
    async with db.db_session() as conn:
        row = await conn.fetchrow("SELECT * FROM plan WHERE code = $1 AND status = 'active'", plan_code)
    if not row:
        return None
    return PlanSummary(
        code=row["code"], name=row["name"], price_cents=row["price_cents"],
        quota_included=row["quota_included"] or {}, features=row["features"] or {},
    )


async def list_plans() -> list[PlanSummary]:
    async with db.db_session() as conn:
        rows = await conn.fetch("SELECT * FROM plan WHERE status = 'active' ORDER BY sort_order")
    return [PlanSummary(code=r["code"], name=r["name"], price_cents=r["price_cents"],
                        quota_included=r["quota_included"] or {}, features=r["features"] or {})
            for r in rows]


async def get_active_subscription(tenant_id: str) -> dict | None:
    async with db.db_session() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM subscription WHERE tenant_id = $1 AND status = 'active' LIMIT 1",
            tenant_id,
        )
    return dict(row) if row else None


async def get_billing_from_ch(tenant_id: str, month: str) -> list[dict]:
    ym = int(month.replace("-", ""))
    rows = query_all(
        "SELECT api_id, toDate(ts) AS day, count() AS calls,"
        "       sum(token_count) AS tokens, sum(latency_ms) AS latency_ms"
        " FROM api_call_log"
        " WHERE tenant_id = %(tenant_id)s AND toYYYYMM(ts) = %(ym)s"
        " GROUP BY api_id, day ORDER BY day, api_id",
        params={"tenant_id": int(tenant_id), "ym": ym},
        force_tenant_id=None,
    )
    return rows


async def get_remaining_calls_today(tenant_id: str) -> int:
    from datetime import datetime
    today = datetime.utcnow().strftime("%Y-%m-%d")
    ym = int(today[:7].replace("-", ""))
    day = int(today[8:10])
    rows = query_all(
        "SELECT count() AS calls FROM api_call_log"
        " WHERE tenant_id = %(tenant_id)s AND toYYYYMM(ts) = %(ym)s"
        "   AND toDayOfMonth(ts) = %(day)s",
        params={"tenant_id": int(tenant_id), "ym": ym, "day": day},
        force_tenant_id=None,
    )
    used = rows[0]["calls"] if rows else 0
    sub = await get_active_subscription(tenant_id)
    plan = await get_plan(sub["plan_code"]) if sub else None
    limit = (plan.quota_included.get("calls_per_day") or 0) if plan else 0
    return max(0, limit - used)
