"""计费数据访问 —— plan/subscription/billing_record CRUD。

⚠️ 列名以 scripts/init-db/05-billing.sql 为准：
  billing_record(subscription_id, period_start, period_end, call_count,
  token_count, base_charge_cents, overage_charge_cents, total_charge_cents,
  status, details, period)
  此前读写两侧用的是一组不存在的列名（plan_name/base_cents/total_calls...），
  出账/查询在真实库上必失败（曾只有空 dry-run 测试掩盖）。
"""

from datetime import datetime
from typing import Any

from apihub_core import db
from apihub_core.errors import ApiError, ErrorCode

from billing.models import BillingRecordItem, SubscriptionInfo

_RECORD_SELECT = """SELECT br.id, br.period, br.call_count, br.token_count,
                           br.base_charge_cents, br.overage_charge_cents,
                           br.total_charge_cents, br.status, br.details, br.created_at,
                           br.tenant_id, p.name AS plan_name
                    FROM billing_record br
                    LEFT JOIN subscription s ON s.id = br.subscription_id
                    LEFT JOIN plan p ON p.code = s.plan_code"""


async def list_active_subscriptions() -> list[SubscriptionInfo]:
    async with db.admin_db_session() as conn:
        rows = await conn.fetch(
            """SELECT s.tenant_id, s.id AS subscription_id, s.plan_code,
                      p.name AS plan_name, p.overage_unit_price,
                      s.period_start, s.period_end, s.status,
                      s.auto_renew, s.quota_included, s.price_cents
               FROM subscription s
               JOIN plan p ON p.code = s.plan_code
               WHERE s.status = 'active'"""
        )
    return [SubscriptionInfo(**dict(r)) for r in rows]


async def get_billing_records(
    tenant_id: str, limit: int = 12, offset: int = 0
) -> tuple[list[BillingRecordItem], int]:
    async with db.db_session() as conn:
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM billing_record WHERE tenant_id = $1", tenant_id
        )
        rows = await conn.fetch(
            f"""{_RECORD_SELECT}
                WHERE br.tenant_id = $1
                ORDER BY br.created_at DESC LIMIT $2 OFFSET $3""",
            tenant_id,
            limit,
            offset,
        )
    return [_row_to_record(r) for r in rows], total


async def get_admin_billing_summary(
    period: str, tenant_search: str = ""
) -> list[BillingRecordItem]:
    async with db.admin_db_session() as conn:
        where = "WHERE br.period = $1"
        params: list[Any] = [period]
        if tenant_search:
            where += " AND br.tenant_id ILIKE $2"
            params.append(f"%{tenant_search}%")
        rows = await conn.fetch(
            f"""{_RECORD_SELECT}
                {where}
                ORDER BY br.created_at DESC""",  # noqa: S608
            *params,
        )
    return [_row_to_record(r) for r in rows]


def _row_to_record(r) -> BillingRecordItem:
    base = r.get("base_charge_cents", 0) or 0
    overage = r.get("overage_charge_cents", 0) or 0
    return BillingRecordItem(
        id=str(r["id"]),
        period=r.get("period", ""),
        plan_name=r.get("plan_name", ""),
        total_calls=r.get("call_count", 0),
        total_tokens=r.get("token_count", 0),
        base_cents=base,
        overage_cents=overage,
        total_cents=r.get("total_charge_cents") or (base + overage),
        status=r.get("status", "pending"),
        details=r.get("details"),
        created_at=r.get("created_at"),
        tenant_id=r.get("tenant_id", ""),
    )


async def insert_billing_record(
    tenant_id: str,
    period: str,
    plan_name: str,
    base_cents: int,
    overage_cents: int,
    details: dict,
    subscription_id: int | None = None,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    status: str = "invoiced",
) -> str:
    """幂等插入：同 (tenant_id, period) 重跑出账覆盖而非重复计费。"""
    async with db.admin_db_session() as conn:
        rid = await conn.fetchval(
            """INSERT INTO billing_record
                   (tenant_id, subscription_id, period_start, period_end,
                    call_count, token_count, base_charge_cents, overage_charge_cents,
                    total_charge_cents, status, details, period)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
               ON CONFLICT (tenant_id, period) DO UPDATE SET
                    call_count = EXCLUDED.call_count,
                    token_count = EXCLUDED.token_count,
                    base_charge_cents = EXCLUDED.base_charge_cents,
                    overage_charge_cents = EXCLUDED.overage_charge_cents,
                    total_charge_cents = EXCLUDED.total_charge_cents,
                    status = EXCLUDED.status,
                    details = EXCLUDED.details
               RETURNING id""",
            tenant_id,
            subscription_id,
            period_start,
            period_end,
            details.get("total_calls", 0),
            details.get("total_tokens", 0),
            base_cents,
            overage_cents,
            base_cents + overage_cents,
            status,
            details,
            period,
        )
    return str(rid)


async def update_subscription_period(
    tenant_id: str, new_start: datetime, new_end: datetime
) -> None:
    async with db.admin_db_session() as conn:
        await conn.execute(
            "UPDATE subscription SET period_start=$1, period_end=$2 WHERE tenant_id=$3",
            new_start,
            new_end,
            tenant_id,
        )


async def insert_job_log(period: str, status: str = "running") -> str:
    async with db.admin_db_session() as conn:
        jid = await conn.fetchval(
            "INSERT INTO billing_job_log (period, status) VALUES ($1,$2) RETURNING id",
            period,
            status,
        )
    return str(jid)


async def check_job_exists(period: str) -> bool:
    async with db.admin_db_session() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM billing_job_log WHERE period=$1 AND status='done' LIMIT 1",
            period,
        )
    return row is not None


async def update_job_log(
    job_id: str,
    tenant_count: int = 0,
    total_base: int = 0,
    total_overage: int = 0,
    status: str = "done",
    error_msg: str = "",
) -> None:
    async with db.admin_db_session() as conn:
        await conn.execute(
            """UPDATE billing_job_log SET tenant_count=$1, total_base=$2, total_overage=$3, status=$4, error_msg=$5, finished_at=NOW() WHERE id=$6""",
            tenant_count,
            total_base,
            total_overage,
            status,
            error_msg,
            job_id,
        )


async def adjust_billing_record(record_id: str, delta_cents: int, reason: str) -> None:
    """手工调账：超额 ±delta，总额同步（reason 进 details 留审计痕迹）。"""
    async with db.admin_db_session() as conn:
        row = await conn.fetchrow(
            """UPDATE billing_record
               SET overage_charge_cents = overage_charge_cents + $1,
                   total_charge_cents = total_charge_cents + $1,
                   status = 'adjusted',
                   details = COALESCE(details, '{}'::jsonb) || jsonb_build_object(
                       'adjust_reasons',
                       COALESCE(details->'adjust_reasons', '[]'::jsonb) || to_jsonb(ARRAY[$2::text]))
               WHERE id = $3
               RETURNING id""",
            delta_cents,
            reason,
            record_id,
        )
        if row is None:
            raise ApiError(ErrorCode.NOT_FOUND, f"billing record {record_id} not found")


async def override_subscription(tenant_id: str, plan_code: str) -> None:
    """改租户套餐。plan_code 必须真实存在（表无 FK，此前任意字符串都能写进去）。"""
    async with db.admin_db_session() as conn:
        exists = await conn.fetchval("SELECT 1 FROM plan WHERE code = $1", plan_code)
        if not exists:
            raise ApiError(ErrorCode.NOT_FOUND, f"plan '{plan_code}' not found")
        updated = await conn.fetchval(
            """UPDATE subscription
               SET plan_code = $1,
                   quota_included = p.quota_included,
                   price_cents = p.price_cents
               FROM plan p
               WHERE p.code = $1 AND subscription.tenant_id = $2
               RETURNING subscription.id""",
            plan_code,
            tenant_id,
        )
        if updated is None:
            raise ApiError(ErrorCode.NOT_FOUND, f"no subscription for tenant {tenant_id}")
