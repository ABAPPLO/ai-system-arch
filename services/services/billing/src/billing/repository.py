"""计费数据访问 —— plan/subscription/billing_record CRUD。"""

from datetime import datetime
from typing import Any

from apihub_core import db

from billing.models import BillingRecordItem, SubscriptionInfo


async def list_active_subscriptions() -> list[SubscriptionInfo]:
    async with db.admin_db_session() as conn:
        rows = await conn.fetch(
            """SELECT s.tenant_id, s.plan_code, p.name AS plan_name,
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
            """SELECT id, period, plan_name, total_calls, total_tokens,
                      base_cents, overage_cents, status, details, created_at
               FROM billing_record WHERE tenant_id = $1
               ORDER BY created_at DESC LIMIT $2 OFFSET $3""",
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
            f"""SELECT br.id, br.period, br.plan_name, br.total_calls, br.total_tokens,
                       br.base_cents, br.overage_cents, br.status, br.tenant_id, br.created_at
                FROM billing_record br {where}
                ORDER BY br.created_at DESC""",  # noqa: S608
            *params,
        )
    return [_row_to_record(r) for r in rows]


def _row_to_record(r) -> BillingRecordItem:
    return BillingRecordItem(
        id=str(r["id"]),
        period=r.get("period", ""),
        plan_name=r.get("plan_name", ""),
        total_calls=r.get("total_calls", 0),
        total_tokens=r.get("total_tokens", 0),
        base_cents=r.get("base_cents", 0),
        overage_cents=r.get("overage_cents", 0),
        total_cents=(r.get("base_cents", 0) or 0) + (r.get("overage_cents", 0) or 0),
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
    status: str = "invoiced",
) -> str:
    async with db.admin_db_session() as conn:
        rid = await conn.fetchval(
            """INSERT INTO billing_record (tenant_id, period, plan_name, base_cents, overage_cents, total_calls, total_tokens, details, status)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id""",
            tenant_id,
            period,
            plan_name,
            base_cents,
            overage_cents,
            details.get("total_calls", 0),
            details.get("total_tokens", 0),
            details,
            status,
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
            job_id,
            tenant_count,
            total_base,
            total_overage,
            status,
            error_msg,
        )


async def adjust_billing_record(record_id: str, delta_cents: int, reason: str) -> None:
    async with db.admin_db_session() as conn:
        await conn.execute(
            "UPDATE billing_record SET overage_cents = overage_cents + $1, status='adjusted' WHERE id=$2",
            delta_cents,
            record_id,
        )


async def override_subscription(tenant_id: str, plan_code: str) -> None:
    async with db.admin_db_session() as conn:
        await conn.execute(
            "UPDATE subscription SET plan_code=$1 WHERE tenant_id=$2", plan_code, tenant_id
        )
