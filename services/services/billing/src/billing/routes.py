from fastapi import APIRouter
from billing import billing_job
from billing.models import BillingAdjustRequest, BillingJobResult, SubscriptionOverrideRequest
from billing.repository import adjust_billing_record, get_admin_billing_summary, get_billing_records, override_subscription

router = APIRouter()


@router.post("/v1/billing/periodic")
async def run_billing(period: str = "", dry_run: bool = False, tenant_ids: list[str] | None = None) -> BillingJobResult:
    return await billing_job.run_billing(period=period, dry_run=dry_run, tenant_ids=tenant_ids)


@router.get("/v1/billing/records")
async def list_billing_records(limit: int = 12, offset: int = 0):
    from apihub_core.auth import require_tenant
    ctx = require_tenant()
    items, total = await get_billing_records(ctx.tenant_id, limit, offset)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/v1/admin/billing/summary")
async def admin_summary(period: str = "", tenant_search: str = ""):
    items = await get_admin_billing_summary(period, tenant_search)
    return {"items": items, "total": len(items), "total_base_cents": sum(i.base_cents for i in items), "total_overage_cents": sum(i.overage_cents for i in items)}


@router.post("/v1/admin/billing/adjust")
async def admin_adjust(req: BillingAdjustRequest):
    await adjust_billing_record(req.record_id, req.delta_cents, req.reason)
    return {"ok": True}


@router.post("/v1/admin/subscription/override")
async def admin_override(req: SubscriptionOverrideRequest):
    await override_subscription(req.tenant_id, req.plan_code)
    return {"ok": True}
