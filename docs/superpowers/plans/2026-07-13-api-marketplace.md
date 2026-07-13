# API 市场化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在已有 plan/subscription/billing_record 表 + Portal 用量看板基础上，构建开发者自助计费全链路。

**Architecture:** 新服务 billing-svc（:8014）处理出账 Job 和 Admin 计费管理；portal-bff 扩展 subscribe/invoices 端点；Portal 前端新增 Plans/Invoices 页 + Usage 增强；Admin 前端新增计费管理页。

**Tech Stack:** Python FastAPI + asyncpg + ClickHouse / React + TS + Tailwind CSS

## Global Constraints

- `admin_db_session()` 用于 billing-svc 出账 Job 和 Admin 端点（平台管理操作）
- `db_session()` 用于 portal-bff 端点（RLS 按租户隔离）
- CH 查询用 `apihub_core.clickhouse.query_all()` / `query_one()`
- 出账 Job 必须幂等（billing_job_log 查重）
- dry_run 模式不能写 PG，只返回预览数据
- `ruff check` + `mypy` clean before commit
- 端口 8014（notification=8012, ai-gateway=8013）

---

### Task 1: DB migration — plan/billing_record 增强 + billing_job_log

**Files:**
- Modify: `scripts/init-db/05-billing.sql`

- [ ] **Step 1: Append migration statements**

```sql
-- ===== Phase 4 API 市场化增强 =====

ALTER TABLE plan ADD COLUMN IF NOT EXISTS overage_unit_price jsonb;
COMMENT ON COLUMN plan.overage_unit_price IS
  '超额单价，如 {"calls_per_1000": 5, "tokens_per_100000": 10}，单位 cents';

ALTER TABLE billing_record ADD COLUMN IF NOT EXISTS details jsonb;
ALTER TABLE billing_record ADD COLUMN IF NOT EXISTS period TEXT;
CREATE INDEX IF NOT EXISTS idx_billing_record_period ON billing_record(period);

CREATE TABLE IF NOT EXISTS billing_job_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    period          TEXT NOT NULL,
    tenant_count    INT NOT NULL DEFAULT 0,
    total_base      BIGINT NOT NULL DEFAULT 0,
    total_overage   BIGINT NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running', 'done', 'failed')),
    error_msg       TEXT,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at     TIMESTAMPTZ
);
```

- [ ] **Step 2: Run migration**

```bash
PGPASSWORD=apihub_dev_pwd psql -h 127.0.0.1 -p 15433 -U apihub -d apihub -f scripts/init-db/05-billing.sql
```

- [ ] **Step 3: Verify tables**

```bash
PGPASSWORD=apihub_dev_pwd psql -h 127.0.0.1 -p 15433 -U apihub -d apihub -c "\d billing_job_log"
```
Expected: Table exists with 7 columns

- [ ] **Step 4: Commit**

```bash
git add scripts/init-db/05-billing.sql
git commit -m "feat(billing): plan 超额定价 + billing_record 增强 + billing_job_log"
```

---

### Task 2: billing-svc 脚手架 + models + repository

**Files:**
- Create: `services/services/billing/pyproject.toml`
- Create: `services/services/billing/src/billing/__init__.py`
- Create: `services/services/billing/src/billing/models.py`
- Create: `services/services/billing/src/billing/repository.py`

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[project]
name = "billing"
version = "0.1.0"
description = "计费服务 —— 出账 Job、账单查询、Admin 计费管理"
requires-python = ">=3.11"
dependencies = ["apihub-core"]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]
```

- [ ] **Step 2: Write `models.py`**

```python
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
```

- [ ] **Step 3: Write `repository.py`**

```python
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


async def get_billing_records(tenant_id: str, limit: int = 12, offset: int = 0) -> tuple[list[BillingRecordItem], int]:
    async with db.db_session() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM billing_record WHERE tenant_id = $1", tenant_id)
        rows = await conn.fetch(
            """SELECT id, period, plan_name, total_calls, total_tokens,
                      base_cents, overage_cents, status, details, created_at
               FROM billing_record WHERE tenant_id = $1
               ORDER BY created_at DESC LIMIT $2 OFFSET $3""",
            tenant_id, limit, offset,
        )
    return [_row_to_record(r) for r in rows], total


async def get_admin_billing_summary(period: str, tenant_search: str = "") -> list[BillingRecordItem]:
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
                ORDER BY br.created_at DESC""",
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


async def insert_billing_record(tenant_id: str, period: str, plan_name: str, base_cents: int, overage_cents: int, details: dict, status: str = "invoiced") -> str:
    async with db.admin_db_session() as conn:
        rid = await conn.fetchval(
            """INSERT INTO billing_record (tenant_id, period, plan_name, base_cents, overage_cents, total_calls, total_tokens, details, status)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id""",
            tenant_id, period, plan_name, base_cents, overage_cents,
            details.get("total_calls", 0), details.get("total_tokens", 0), details, status,
        )
    return str(rid)


async def update_subscription_period(tenant_id: str, new_start: datetime, new_end: datetime) -> None:
    async with db.admin_db_session() as conn:
        await conn.execute("UPDATE subscription SET period_start=$1, period_end=$2 WHERE tenant_id=$3", new_start, new_end, tenant_id)


async def insert_job_log(period: str, status: str = "running") -> str:
    async with db.admin_db_session() as conn:
        jid = await conn.fetchval("INSERT INTO billing_job_log (period, status) VALUES ($1,$2) RETURNING id", period, status)
    return str(jid)


async def update_job_log(job_id: str, tenant_count: int = 0, total_base: int = 0, total_overage: int = 0, status: str = "done", error_msg: str = "") -> None:
    async with db.admin_db_session() as conn:
        await conn.execute(
            """UPDATE billing_job_log SET tenant_count=$1, total_base=$2, total_overage=$3, status=$4, error_msg=$5, finished_at=NOW() WHERE id=$6""",
            job_id, tenant_count, total_base, total_overage, status, error_msg,
        )


async def adjust_billing_record(record_id: str, delta_cents: int, reason: str) -> None:
    async with db.admin_db_session() as conn:
        await conn.execute("UPDATE billing_record SET overage_cents = overage_cents + $1, status='adjusted' WHERE id=$2", delta_cents, record_id)


async def override_subscription(tenant_id: str, plan_code: str) -> None:
    async with db.admin_db_session() as conn:
        await conn.execute("UPDATE subscription SET plan_code=$1 WHERE tenant_id=$2", plan_code, tenant_id)
```

- [ ] **Step 4: Verify import**

```bash
cd services/services/billing && python -c "from billing.models import *; from billing.repository import *; print('OK')"
```

- [ ] **Step 5: Commit**

```bash
git add services/services/billing/
git commit -m "feat(billing): 计费服务脚手架 + models + repository"
```

---

### Task 3: billing_job.py — 出账核心逻辑

**Files:**
- Create: `services/services/billing/src/billing/billing_job.py`

- [ ] **Step 1: Write `billing_job.py`**

```python
"""出账核心逻辑：查 CH 用量 → 算超额 → 写 billing_record。"""

from datetime import datetime, timedelta
from typing import Any
from apihub_core.clickhouse import query_all
from apihub_core.logging import get_logger
from billing.models import BillingJobResult, BillingPreviewRecord
from billing.repository import (
    insert_billing_record, insert_job_log, list_active_subscriptions,
    update_job_log, update_subscription_period,
)

log = get_logger(__name__)


def _parse_period(period: str) -> tuple[datetime, datetime]:
    y, m = int(period[:4]), int(period[5:7])
    start = datetime(y, m, 1)
    end = datetime(y + 1, 1, 1) if m == 12 else datetime(y, m + 1, 1)
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


async def run_billing(period: str = "", dry_run: bool = False, tenant_ids: list[str] | None = None) -> BillingJobResult:
    period = period or _default_period()
    period_start, period_end = _parse_period(period)
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
            overage_charge = (
                _calc_overage(actual_calls, quota_calls, price_calls, 1000)
                + _calc_overage(actual_tokens, quota_tokens, price_tokens, 100000)
            )

            records.append(BillingPreviewRecord(
                tenant_id=sub.tenant_id, plan_code=sub.plan_code, plan_name=sub.plan_name,
                total_calls=actual_calls or 0, total_tokens=actual_tokens or 0,
                quota_calls=quota_calls, quota_tokens=quota_tokens,
                overage_calls=overage_calls, overage_tokens=overage_tokens,
                base_cents=base_cents, overage_cents=overage_charge,
            ))

            if not dry_run:
                details = {
                    "total_calls": actual_calls, "total_tokens": actual_tokens,
                    "overage_calls": overage_calls, "overage_tokens": overage_tokens,
                    "unit_price_calls": price_calls, "unit_price_tokens": price_tokens,
                    "plan_code": sub.plan_code,
                    "period_start": period_start.isoformat(), "period_end": period_end.isoformat(),
                }
                await insert_billing_record(
                    tenant_id=sub.tenant_id, period=period, plan_name=sub.plan_name,
                    base_cents=base_cents, overage_cents=overage_charge, details=details,
                )
                await update_subscription_period(sub.tenant_id, period_end, period_end + timedelta(days=30))
        except Exception as e:
            log.error("billing_tenant_error", tenant_id=sub.tenant_id, error=str(e))
            if not dry_run:
                await update_job_log(job_id, status="failed", error_msg=str(e)[:500])
            raise

    total_base = sum(r.base_cents for r in records)
    total_overage = sum(r.overage_cents for r in records)
    if not dry_run:
        await update_job_log(job_id, tenant_count=len(records), total_base=total_base, total_overage=total_overage)

    return BillingJobResult(job_id=job_id, period=period, total_tenants=len(records), total_base_cents=total_base, total_overage_cents=total_overage, records=records)
```

- [ ] **Step 2: Commit**

```bash
git add services/services/billing/src/billing/billing_job.py
git commit -m "feat(billing): 出账 Job（查 CH + 算超额 + 写 PG）"
```

---

### Task 4: billing-svc routes + main

**Files:**
- Create: `services/services/billing/src/billing/routes.py`
- Create: `services/services/billing/src/billing/main.py`

- [ ] **Step 1: Write `routes.py`**

```python
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
```

- [ ] **Step 2: Write `main.py`**

```python
from apihub_core import create_app
from billing.routes import router

def _build(app):
    app.include_router(router)

app = create_app(service_name="billing", build_routes=_build, skip_auth_paths=("/health", "/metrics", "/docs", "/openapi.json"))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("billing.main:app", host="0.0.0.0", port=8014, workers=1, log_level="info")
```

- [ ] **Step 3: Verify**

```bash
python -c "from billing.main import app; print(f'routes: {len(app.routes)}'); print('OK')"
```

- [ ] **Step 4: Commit**

```bash
git add services/services/billing/src/billing/routes.py services/services/billing/src/billing/main.py
git commit -m "feat(billing): FastAPI 端点 + 入口"
```

---

### Task 5: billing-svc 单测

**Files:**
- Create: `services/services/billing/tests/__init__.py`
- Create: `services/services/billing/tests/conftest.py`
- Create: `services/services/billing/tests/test_billing_job.py`
- Create: `services/services/billing/tests/test_routes.py`

- [ ] **Step 1: Write `conftest.py`**

```python
import os
for k in ("all_proxy","ALL_PROXY","http_proxy","HTTP_PROXY","https_proxy","HTTPS_PROXY","no_proxy","NO_PROXY"):
    os.environ.pop(k, None)
os.environ.setdefault("PG_HOST","localhost"); os.environ.setdefault("PG_USER","apihub")
os.environ.setdefault("PG_PASSWORD","test"); os.environ.setdefault("REDIS_HOST","localhost")
os.environ.setdefault("ENV","test")
import pytest
from apihub_core.config import get_settings; get_settings.cache_clear()
from httpx import ASGITransport, AsyncClient

@pytest.fixture(autouse=True)
def clear_cache():
    get_settings.cache_clear(); yield; get_settings.cache_clear()

@pytest.fixture
def client():
    from billing.main import app
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
```

- [ ] **Step 2: Write `test_billing_job.py`**

```python
import pytest
from billing.billing_job import _parse_period, _calc_overage

class TestBillingCore:
    def test_parse_period(self):
        s, e = _parse_period("2026-07")
        assert s.year == 2026 and s.month == 7 and s.day == 1
        assert e.month == 8

    def test_no_overage(self):
        assert _calc_overage(100, 200, 5, 1000) == 0

    def test_with_overage(self):
        assert _calc_overage(1500, 1000, 5, 1000) == 5

    def test_multi_unit(self):
        assert _calc_overage(3500, 1000, 5, 1000) == 15

    @pytest.mark.asyncio
    async def test_dry_run_no_subs(self, monkeypatch):
        async def mock_list(): return []
        monkeypatch.setattr("billing.billing_job.list_active_subscriptions", mock_list)
        result = await billing_job.run_billing(period="2026-07", dry_run=True)
        assert result.total_tenants == 0
```

- [ ] **Step 3: Write `test_routes.py`**

```python
import pytest

@pytest.mark.asyncio
async def test_health(client):
    resp = await client.get("/health/live")
    assert resp.status_code == 200

@pytest.mark.asyncio
async def test_periodic_dry_run(client, monkeypatch):
    async def mock_run(**kw):
        from billing.models import BillingJobResult
        return BillingJobResult(period="2026-07", total_tenants=0)
    monkeypatch.setattr("billing.routes.billing_job.run_billing", mock_run)
    resp = await client.post("/v1/billing/periodic?dry_run=true")
    assert resp.status_code == 200
    assert resp.json()["period"] == "2026-07"
```

- [ ] **Step 4: Run tests**

```bash
cd services/services/billing && python -m pytest tests/ -v
```
Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add services/services/billing/tests/
git commit -m "test(billing): 出账 + 路由单测"
```

---

### Task 6: portal-bff 扩展 — subscribe + invoices

**Files:**
- Modify: `services/services/portal/src/portal/models.py`
- Modify: `services/services/portal/src/portal/repository.py`
- Modify: `services/services/portal/src/portal/routes.py`
- Modify: `services/libs/apihub-core/src/apihub_core/config.py`

- [ ] **Step 1: Add models**

In `portal/models.py` add:
```python
class SubscribeRequest(BaseModel):
    plan_code: str
    action: str = "upgrade"
```

- [ ] **Step 2: Add repository functions**

In `portal/repository.py` add:
```python
async def subscribe_plan(tenant_id: str, plan_code: str) -> dict:
    async with db.admin_db_session() as conn:
        await conn.execute("UPDATE subscription SET plan_code=$1 WHERE tenant_id=$2", plan_code, tenant_id)
    return {"ok": True, "plan_code": plan_code}

async def get_invoices(tenant_id: str, limit: int = 12, offset: int = 0) -> dict:
    async with db.db_session() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM billing_record WHERE tenant_id=$1", tenant_id)
        rows = await conn.fetch(
            """SELECT id, period, plan_name, total_calls, total_tokens,
                      base_cents, overage_cents, status, created_at
               FROM billing_record WHERE tenant_id=$1
               ORDER BY created_at DESC LIMIT $2 OFFSET $3""",
            tenant_id, limit, offset,
        )
    items = [{
        "id": str(r["id"]), "period": r.get("period",""), "plan_name": r.get("plan_name",""),
        "total_calls": r.get("total_calls",0), "total_tokens": r.get("total_tokens",0),
        "base_cents": r.get("base_cents",0), "overage_cents": r.get("overage_cents",0),
        "total_cents": (r.get("base_cents",0)or0)+(r.get("overage_cents",0)or0),
        "status": r.get("status",""),
        "created_at": r["created_at"].isoformat() if r.get("created_at") else "",
    } for r in rows]
    return {"items": items, "total": total}
```

- [ ] **Step 3: Add routes**

In `portal/routes.py` (inside the app routes block):
```python
    @app.post("/v1/portal/subscribe")
    async def portal_subscribe(payload: SubscribeRequest):
        ctx = require_tenant()
        return await repository.subscribe_plan(ctx.tenant_id, payload.plan_code)

    @app.get("/v1/portal/invoices")
    async def portal_invoices(limit: int = 12, offset: int = 0):
        ctx = require_tenant()
        return await repository.get_invoices(ctx.tenant_id, limit, offset)
```

- [ ] **Step 4: Commit**

```bash
git add services/services/portal/src/portal/ services/libs/apihub-core/src/apihub_core/config.py
git commit -m "feat(portal): subscribe + invoices 端点"
```

---

### Task 7: Portal 前端 — Plans + Invoices + Usage 增强

**Files:**
- Create: `frontend/portal/src/pages/Plans.tsx`
- Create: `frontend/portal/src/pages/Invoices.tsx`
- Modify: `frontend/portal/src/pages/Usage.tsx`
- Modify: `frontend/portal/src/App.tsx`

- [ ] **Step 1: Write `Plans.tsx`**

```typescript
import { useEffect, useState } from 'react';
import { api } from '../api/client';

interface PlanInfo {
  code: string; name: string; description: string | null;
  price_cents: number; quota_included: Record<string, number>;
  features: Record<string, boolean> | null; sort_order: number;
}

const FEAT_LABELS: Record<string, string> = { api_catalog: 'API 目录', try_it: '在线调试', sdk: 'SDK 下载' };

export function Plans() {
  const [plans, setPlans] = useState<PlanInfo[]>([]);
  const [currentPlan, setCurrentPlan] = useState('');
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState('');

  useEffect(() => {
    setLoading(true); setErr('');
    Promise.all([
      api.get<PlanInfo[]>('/v1/portal/plans'),
      api.get<{plan_code: string}>('/v1/portal/subscription'),
    ]).then(([p, sub]) => { setPlans(p); setCurrentPlan(sub.plan_code); })
      .catch(e => setErr(e instanceof Error ? e.message : '加载失败'))
      .finally(() => setLoading(false));
  }, []);

  const doUpgrade = async (code: string) => {
    if (!confirm(`确认升级到 ${plans.find(p=>p.code===code)?.name}？`)) return;
    try {
      await api.post('/v1/portal/subscribe', { plan_code: code, action: 'upgrade' });
      setCurrentPlan(code); alert('升级成功');
    } catch (e) { alert('升级失败'); }
  };

  if (loading) return <div className="flex justify-center py-12"><div className="animate-spin w-8 h-8 border-4 border-blue-500 border-t-transparent rounded-full" /></div>;
  if (err) return <div className="text-red-600 p-4">{err}</div>;

  return (
    <div className="max-w-5xl mx-auto p-4">
      <h1 className="text-2xl font-bold mb-2">选择 Plan</h1>
      <p className="text-gray-500 mb-6">按需选择，随时升级。</p>
      <div className="grid grid-cols-4 gap-4 mb-8">
        {plans.map(p => {
          const isCurrent = p.code === currentPlan;
          const q = p.quota_included;
          return (
            <div key={p.code} className={`border rounded-lg p-4 flex flex-col ${isCurrent ? 'border-blue-500 ring-2 ring-blue-200' : ''}`}>
              <h3 className="text-lg font-bold">{p.name}</h3>
              <p className="text-2xl font-bold mt-2">{p.price_cents > 0 ? `¥${p.price_cents/100}` : '免费'}</p>
              <p className="text-sm text-gray-400 mb-4">{p.price_cents > 0 ? '/月' : ''}</p>
              <ul className="text-sm space-y-1 mb-4 flex-1">
                <li>📞 {(q.calls_per_day || 0).toLocaleString()} 次/日</li>
                <li>🔤 {(q.tokens_per_month || 0).toLocaleString()} Token/月</li>
                {Object.entries(FEAT_LABELS).map(([k, v]) => (<li key={k}>{p.features?.[k] ? '✅' : '❌'} {v}</li>))}
              </ul>
              {isCurrent ? <span className="text-center text-sm bg-blue-100 text-blue-700 py-1 rounded">当前 Plan</span>
                : p.code === 'enterprise' ? <a href="mailto:sales@apihub.com" className="text-center text-sm bg-gray-100 py-1 rounded block">联系销售</a>
                : <button className="bg-blue-600 text-white py-1 rounded text-sm" onClick={() => doUpgrade(p.code)}>立即升级</button>}
            </div>
          );
        })}
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Write `Invoices.tsx`**

```typescript
import { useEffect, useState } from 'react';
import { api } from '../api/client';

interface InvoiceItem {
  id: string; period: string; plan_name: string;
  total_calls: number; total_tokens: number;
  base_cents: number; overage_cents: number; total_cents: number;
  status: string; created_at: string;
}

function fmtCents(c: number): string { return `¥${(c / 100).toFixed(2)}`; }

const STATUS_BADGE: Record<string, string> = { pending: 'bg-yellow-100 text-yellow-700', invoiced: 'bg-green-100 text-green-700', adjusted: 'bg-blue-100 text-blue-700' };

export function Invoices() {
  const [items, setItems] = useState<InvoiceItem[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const limit = 12;

  useEffect(() => {
    setLoading(true);
    api.get<{items: InvoiceItem[]; total: number}>('/v1/portal/invoices', { limit, offset })
      .then(r => { setItems(r.items); setTotal(r.total); })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [offset]);

  if (loading) return <div className="flex justify-center py-12"><div className="animate-spin w-8 h-8 border-4 border-blue-500 border-t-transparent rounded-full" /></div>;

  return (
    <div className="max-w-4xl mx-auto p-4">
      <h1 className="text-2xl font-bold mb-4">账单历史</h1>
      {items.length === 0 ? <div className="text-center py-12 text-gray-400"><p>暂无账单记录</p></div> : (
        <>
          <table className="w-full text-sm">
            <thead><tr className="border-b"><th className="text-left py-2">周期</th><th>Plan</th><th className="text-right">调用量</th><th className="text-right">Token</th><th className="text-right">费用</th><th className="text-right">状态</th></tr></thead>
            <tbody>{items.map(i => (
              <tr key={i.id} className="border-b hover:bg-gray-50">
                <td className="py-2">{i.period}</td>
                <td className="text-center">{i.plan_name}</td>
                <td className="text-right">{(i.total_calls||0).toLocaleString()}</td>
                <td className="text-right">{(i.total_tokens||0).toLocaleString()}</td>
                <td className="text-right">{fmtCents(i.total_cents)}</td>
                <td className="text-right"><span className={`text-xs px-1.5 py-0.5 rounded ${STATUS_BADGE[i.status]||''}`}>{i.status}</span></td>
              </tr>
            ))}</tbody>
          </table>
          {total > limit && (
            <div className="flex justify-center gap-2 mt-4">
              <button disabled={offset===0} onClick={() => setOffset(offset-limit)} className="px-3 py-1 border rounded disabled:opacity-50">上一页</button>
              <button disabled={offset+limit>=total} onClick={() => setOffset(offset+limit)} className="px-3 py-1 border rounded disabled:opacity-50">下一页</button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
```

- [ ] **Step 3: Enhance `Usage.tsx`**

After the `data.month` line in Usage.tsx, add Plan info card:
```tsx
<div className="border rounded-lg p-4 mb-4 bg-blue-50">
  <div className="flex items-center justify-between">
    <div>
      <p className="text-sm text-gray-500">当前 Plan</p>
      <p className="text-lg font-bold">{data.plan.name}</p>
    </div>
    <a href="/plans" className="text-blue-600 text-sm">升级 Plan →</a>
  </div>
</div>
```

Update ProgressBar to change color at thresholds:
```tsx
function ProgressBar({ used, total }: { used: number; total: number }) {
  const pct = total > 0 ? Math.min(100, (used / total) * 100) : 0;
  const color = pct >= 100 ? 'bg-red-500' : pct >= 80 ? 'bg-orange-500' : 'bg-blue-500';
  return (
    <div className="w-full bg-gray-200 rounded h-2 mt-1">
      <div className={`${color} h-2 rounded`} style={{ width: `${pct}%` }} />
      {pct >= 100 && <p className="text-xs text-red-600 mt-1">超出配额</p>}
    </div>
  );
}
```

- [ ] **Step 4: Update `App.tsx`**

Add imports:
```typescript
import { Plans } from './pages/Plans';
import { Invoices } from './pages/Invoices';
```

Add routes:
```typescript
<Route path="/plans" element={auth ? <Plans /> : <Navigate to="/login" />} />
<Route path="/invoices" element={auth ? <Invoices /> : <Navigate to="/login" />} />
```

- [ ] **Step 5: Commit**

```bash
git add frontend/portal/src/pages/Plans.tsx frontend/portal/src/pages/Invoices.tsx frontend/portal/src/pages/Usage.tsx frontend/portal/src/App.tsx
git commit -m "feat(portal-frontend): Plans 对比 + Invoices 账单页 + Usage 增强"
```

---

### Task 8: Admin 计费管理

**Files:**
- Create: `frontend/admin/src/pages/Billing.tsx`
- Modify: `frontend/admin/src/App.tsx`

- [ ] **Step 1: Write `Billing.tsx`**

```typescript
import { useEffect, useState } from 'react';
import { api } from '../api/client';

function fmtCents(c: number): string { return `¥${(c / 100).toFixed(2)}`; }

export function Billing() {
  const [items, setItems] = useState<any[]>([]);
  const [period, setPeriod] = useState(() => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}`; });
  const [search, setSearch] = useState('');
  const [loading, setLoading] = useState(true);

  const fetchData = async () => {
    setLoading(true);
    try {
      const r = await api.get<any>('/v1/admin/billing/summary', { period, search });
      setItems(r.items || []);
    } catch (e) { console.error(e); }
    finally { setLoading(false); }
  };

  useEffect(() => { fetchData(); }, [period, search]);

  return (
    <div className="p-4">
      <h1 className="text-2xl font-bold mb-4">计费管理</h1>
      <div className="flex gap-2 mb-4 items-center">
        <input type="month" value={period} onChange={e => setPeriod(e.target.value)} className="border rounded px-3 py-1" />
        <button onClick={fetchData} className="bg-blue-600 text-white px-4 py-1 rounded">刷新</button>
        <input placeholder="搜索租户..." value={search} onChange={e => setSearch(e.target.value)} className="border rounded px-3 py-1 ml-auto" />
      </div>
      {loading ? <div className="animate-spin w-6 h-6 border-2 border-blue-500 border-t-transparent rounded-full" /> : (
        <table className="w-full text-sm">
          <thead><tr className="border-b"><th className="text-left py-2">租户</th><th>Plan</th><th className="text-right">调用量</th><th className="text-right">费用</th><th className="text-right">状态</th></tr></thead>
          <tbody>{items.map((i, idx) => (
            <tr key={i.tenant_id || idx} className="border-b hover:bg-gray-50">
              <td className="py-2">{i.tenant_id}</td>
              <td className="text-center">{i.plan_name}</td>
              <td className="text-right">{(i.total_calls||0).toLocaleString()}</td>
              <td className="text-right">{fmtCents((i.base_cents||0)+(i.overage_cents||0))}</td>
              <td className="text-right"><span className={`text-xs px-1.5 py-0.5 rounded ${i.status === 'invoiced' ? 'bg-green-100 text-green-700' : i.status === 'pending' ? 'bg-yellow-100 text-yellow-700' : 'bg-gray-100'}`}>{i.status}</span></td>
            </tr>
          ))}</tbody>
        </table>
      )}
    </div>
  );
}
```

- [ ] **Step 2: Update `App.tsx`**

```typescript
import { Billing } from './pages/Billing';
// Add route:
<Route path="/admin/billing" element={<Billing />} />
```

- [ ] **Step 3: Commit**

```bash
git add frontend/admin/src/pages/Billing.tsx frontend/admin/src/App.tsx
git commit -m "feat(admin): 计费管理页"
```

---

### Task 9: Makefile + Dockerfile + lint

**Files:**
- Modify: `Makefile`
- Create: `services/services/billing/Dockerfile`

- [ ] **Step 1: Makefile target**

After `run-ai-gateway:`:
```makefile
run-billing:  ## 本地启动 billing-svc（出账 + 计费管理，需要 PG + CH）
	uvicorn billing.main:app --reload --port 8014
```
Add `run-billing` to the help line.

- [ ] **Step 2: Dockerfile**

```dockerfile
FROM python:3.14-slim AS builder
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends build-essential libpq-dev libffi-dev && rm -rf /var/lib/apt/lists/*
RUN useradd -m -u 1000 apihub && chown -R apihub:apihub /app
USER apihub
COPY --chown=apihub:apihub services/libs/apihub-core /tmp/apihub-core
RUN pip install --user /tmp/apihub-core
COPY --chown=apihub:apihub services/services/billing /tmp/billing
RUN pip install --user /tmp/billing
FROM python:3.14-slim
WORKDIR /app
COPY --from=builder /home/apihub/.local /root/.local
ENV PATH=/root/.local/bin:$PATH
EXPOSE 8014
CMD ["uvicorn", "billing.main:app", "--host", "0.0.0.0", "--port", "8014", "--workers", "1"]
```

- [ ] **Step 3: Lint**

```bash
ruff check services/services/billing/
mypy services/services/billing/
```
Expected: clean

- [ ] **Step 4: Commit**

```bash
git add Makefile services/services/billing/Dockerfile
git commit -m "chore(billing): Makefile + Dockerfile"
```
