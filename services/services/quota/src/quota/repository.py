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
    if isinstance(raw, (int, float)):
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
