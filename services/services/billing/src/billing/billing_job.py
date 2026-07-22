"""出账核心逻辑：查 CH 用量 → 算超额 → 写 billing_record。"""

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


def _parse_period(period: str) -> tuple[datetime, datetime]:
    y, m = int(period[:4]), int(period[5:7])
    start = datetime(y, m, 1, tzinfo=UTC)
    end = datetime(y + 1, 1, 1, tzinfo=UTC) if m == 12 else datetime(y, m + 1, 1, tzinfo=UTC)
    return start, end


def _default_period() -> str:
    now = datetime.utcnow()
    last = now.replace(day=1) - timedelta(days=1)
    return last.strftime("%Y-%m")


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

    if not dry_run:
        existing = await check_job_exists(period)
        if existing:
            log.warning("billing_already_run", period=period)
            return BillingJobResult(period=period, total_tenants=0, records=[])

    subs = await list_active_subscriptions()
    if tenant_ids:
        subs = [s for s in subs if s.tenant_id in tenant_ids]

    records: list[BillingPreviewRecord] = []
    job_id = "" if dry_run else await insert_job_log(period)

    for sub in subs:
        try:
            ch_rows = await query_all(
                """SELECT sum(is_success) as calls, sum(token_total) as tokens
                   FROM api_call_log
                   WHERE tenant_id = %(tenant)s AND ts >= %(start)s AND ts < %(end)s""",
                {"tenant": sub.tenant_id, "start": period_start, "end": period_end},
            )
            actual_calls = ch_rows[0]["calls"] if ch_rows else 0
            actual_tokens = ch_rows[0]["tokens"] if ch_rows else 0

            qi = sub.quota_included or {}
            quota_calls = (qi.get("calls_per_day", 0) or 0) * 30
            quota_tokens = qi.get("tokens_per_month", 0) or 0

            overage_calls = max(0, actual_calls - quota_calls)
            overage_tokens = max(0, actual_tokens - quota_tokens)

            base_cents = sub.price_cents or 0
            price_calls = 5
            price_tokens = 10
            overage_charge = _calc_overage(
                actual_calls, quota_calls, price_calls, 1000
            ) + _calc_overage(actual_tokens, quota_tokens, price_tokens, 100000)

            records.append(
                BillingPreviewRecord(
                    tenant_id=sub.tenant_id,
                    plan_code=sub.plan_code,
                    plan_name=sub.plan_name,
                    total_calls=actual_calls or 0,
                    total_tokens=actual_tokens or 0,
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
                )
                await update_subscription_period(
                    sub.tenant_id, period_end, period_end + timedelta(days=30)
                )
        except Exception as e:
            log.error("billing_tenant_error", tenant_id=sub.tenant_id, error=str(e))
            if not dry_run:
                await update_job_log(job_id, status="failed", error_msg=str(e)[:500])
            raise

    total_base = sum(r.base_cents for r in records)
    total_overage = sum(r.overage_cents for r in records)
    if not dry_run:
        await update_job_log(
            job_id, tenant_count=len(records), total_base=total_base, total_overage=total_overage
        )

    return BillingJobResult(
        job_id=job_id,
        period=period,
        total_tenants=len(records),
        total_base_cents=total_base,
        total_overage_cents=total_overage,
        records=records,
    )
