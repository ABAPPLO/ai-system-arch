"""计费 Pydantic 模型。"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class SubscriptionInfo(BaseModel):
    tenant_id: str
    plan_code: str
    plan_name: str = ""
    period_start: datetime | None = None
    period_end: datetime | None = None
    status: str = "active"
    auto_renew: bool = True
    quota_included: dict[str, Any] = {}
    price_cents: int = 0


class BillingRecordItem(BaseModel):
    id: int | str = ""
    period: str = ""
    plan_name: str = ""
    total_calls: int = 0
    total_tokens: int = 0
    base_cents: int = 0
    overage_cents: int = 0
    total_cents: int = 0
    status: str = "pending"
    details: dict[str, Any] | None = None
    created_at: datetime | None = None
    tenant_id: str = ""


class BillingPreviewRecord(BaseModel):
    tenant_id: str
    plan_code: str
    plan_name: str = ""
    total_calls: int = 0
    total_tokens: int = 0
    quota_calls: int = 0
    quota_tokens: int = 0
    overage_calls: int = 0
    overage_tokens: int = 0
    base_cents: int = 0
    overage_cents: int = 0


class BillingJobResult(BaseModel):
    job_id: str = ""
    period: str
    total_tenants: int = 0
    total_base_cents: int = 0
    total_overage_cents: int = 0
    records: list[BillingPreviewRecord] = []


class BillingAdjustRequest(BaseModel):
    record_id: str
    delta_cents: int
    reason: str


class SubscriptionOverrideRequest(BaseModel):
    tenant_id: str
    plan_code: str
    reason: str
