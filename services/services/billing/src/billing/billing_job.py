"""出账核心逻辑：查 CH 用量 → 算超额 → 写 billing_record。

修复要点（此前在真实环境必失败）：
  1. CH 查询显式 force_tenant_id=None（跨租户管理聚合；默认 sentinel 会因
     SQL 缺 %(tenant_id)s 被租户护栏 ValueError 掉）。
  2. INSERT/UPDATE 列名对齐 05-billing.sql 真实 schema（repository 侧）。
  3. 配额按账期实际天数算（不再硬编码 30 天）；超额单价读 plan.overage_unit_price。
  4. 账期推进用自然月边界（不再 +30 天漂移）。
  5. 单租户失败不再中断整批：记 error 继续下一个，job 末尾标 failed；
     insert 侧 ON CONFLICT (tenant_id, period) 保证重跑幂等。
"""

from datetime import UTC, datetime, timedelta

from apihub_core.clickhouse import query_all
from apihub_core.logging import get_logger

from billing.models import BillingJobResult, BillingPreviewRecord
from billing.repository import (
    check_job_exists,
    insert_billing_record,
    insert_job_log,
    list_active_subscriptions,
    update_job_log,
    update_subscription_period,
)

log = get_logger(__name__)

# plan.overage_unit_price 缺省时的兜底单价（cents）
_DEFAULT_PRICE_CALLS_PER_1K = 5
_DEFAULT_PRICE_TOKENS_PER_100K = 10


def _parse_period(period: str) -> tuple[datetime, datetime]:
    y, m = int(period[:4]), int(period[5:7])
    start = datetime(y, m, 1, tzinfo=UTC)
    end = datetime(y + 1, 1, 1, tzinfo=UTC) if m == 12 else datetime(y, m + 1, 1, tzinfo=UTC)
    return start, end


def _default_period() -> str:
    now = datetime.now(UTC)
    last = now.replace(day=1) - timedelta(days=1)
    return last.strftime("%Y-%m")


def _next_month_start(end: datetime) -> datetime:
    """end 所在月的下一个月 1 号（账期推进用自然月边界）。"""
    if end.month == 12:
        return datetime(end.year + 1, 1, 1, tzinfo=UTC)
    return datetime(end.year, end.month + 1, 1, tzinfo=UTC)


def _calc_overage(actual: int, quota: int, price_per_unit: int, unit_size: int) -> int:
    if quota <= 0 or actual <= quota:
        return 0
    overage = actual - quota
    units = (overage + unit_size - 1) // unit_size
    return units * price_per_unit


async def run_billing(
    period: str = "", dry_run: bool = False, tenant_ids: list[str] | None = None
) -> BillingJobResult:
    period = period or _default_period()
    period_start, period_end = _parse_period(period)
    days_in_period = (period_end - period_start).days

    if not dry_run:
        existing = await check_job_exists(period)
        if existing:
            log.warning("billing_already_run", period=period)
            return BillingJobResult(period=period, total_tenants=0, records=[])

    subs = await list_active_subscriptions()
    if tenant_ids:
        subs = [s for s in subs if s.tenant_id in tenant_ids]

    records: list[BillingPreviewRecord] = []
    errors: list[str] = []
    job_id = "" if dry_run else await insert_job_log(period)

    for sub in subs:
        try:
            # 跨租户管理聚合：显式 admin 域（护栏放行 + 记审计日志）。
            # 注意 query_all 是同步函数（clickhouse_connect 同步 client），不可 await
            ch_rows = query_all(
                """SELECT sum(is_success) AS calls, sum(token_total) AS tokens
                   FROM api_call_log
                   WHERE tenant_id = %(tenant_id)s AND ts >= %(start)s AND ts < %(end)s""",
                {
                    "tenant_id": sub.tenant_id,
                    "start": period_start,
                    "end": period_end,
                },
                force_tenant_id=None,
            )
            actual_calls = int(ch_rows[0]["calls"] or 0) if ch_rows else 0
            actual_tokens = int(ch_rows[0]["tokens"] or 0) if ch_rows else 0

            qi = sub.quota_included or {}
            quota_calls = (qi.get("calls_per_day", 0) or 0) * days_in_period
            quota_tokens = qi.get("tokens_per_month", 0) or 0

            overage_calls = max(0, actual_calls - quota_calls)
            overage_tokens = max(0, actual_tokens - quota_tokens)

            # 超额单价：plan.overage_unit_price（{"calls_per_1000": x, "tokens_per_100000": y}），
            # 未配置走平台兜底价
            prices = sub.overage_unit_price or {}
            price_calls = prices.get("calls_per_1000", _DEFAULT_PRICE_CALLS_PER_1K)
            price_tokens = prices.get("tokens_per_100000", _DEFAULT_PRICE_TOKENS_PER_100K)

            base_cents = sub.price_cents or 0
            overage_charge = _calc_overage(
                actual_calls, quota_calls, price_calls, 1000
            ) + _calc_overage(actual_tokens, quota_tokens, price_tokens, 100000)

            records.append(
                BillingPreviewRecord(
                    tenant_id=sub.tenant_id,
                    plan_code=sub.plan_code,
                    plan_name=sub.plan_name,
                    total_calls=actual_calls,
                    total_tokens=actual_tokens,
                    quota_calls=quota_calls,
                    quota_tokens=quota_tokens,
                    overage_calls=overage_calls,
                    overage_tokens=overage_tokens,
                    base_cents=base_cents,
                    overage_cents=overage_charge,
                )
            )

            if not dry_run:
                details = {
                    "total_calls": actual_calls,
                    "total_tokens": actual_tokens,
                    "overage_calls": overage_calls,
                    "overage_tokens": overage_tokens,
                    "unit_price_calls": price_calls,
                    "unit_price_tokens": price_tokens,
                    "plan_code": sub.plan_code,
                    "days_in_period": days_in_period,
                    "period_start": period_start.isoformat(),
                    "period_end": period_end.isoformat(),
                }
                await insert_billing_record(
                    tenant_id=sub.tenant_id,
                    period=period,
                    plan_name=sub.plan_name,
                    base_cents=base_cents,
                    overage_cents=overage_charge,
                    details=details,
                    subscription_id=sub.subscription_id,
                    period_start=period_start,
                    period_end=period_end,
                )
                await update_subscription_period(
                    sub.tenant_id, period_end, _next_month_start(period_end)
                )
        except Exception as e:
            # 单租户失败不炸整批：留痕、继续，job 末尾统一标 failed（重跑幂等，见 INSERT ON CONFLICT）
            log.error("billing_tenant_error", tenant_id=sub.tenant_id, error=str(e))
            errors.append(f"{sub.tenant_id}: {e}")
            continue

    total_base = sum(r.base_cents for r in records)
    total_overage = sum(r.overage_cents for r in records)
    if not dry_run:
        await update_job_log(
            job_id,
            tenant_count=len(records),
            total_base=total_base,
            total_overage=total_overage,
            status="failed" if errors else "done",
            error_msg="; ".join(errors)[:500],
        )

    return BillingJobResult(
        job_id=job_id,
        period=period,
        total_tenants=len(records),
        total_base_cents=total_base,
        total_overage_cents=total_overage,
        records=records,
    )
