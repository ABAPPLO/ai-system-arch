# Phase 3 第三切片「配额与计费」Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让外部开发者在 Portal 中查看当前 Plan、API 调用用量（次数/Token）、Plan 对比，为真计费打地基。

**Architecture:** quota 服务从 ClickHouse 聚合用量 + T+1 定时任务写入 billing_record；portal-bff 新增 3 个代理端点；Portal 前端新增用量看板页。

**Tech Stack:** Python FastAPI + asyncpg + ClickHouse / React + TS + Vite + Tailwind CSS + Zustand

## Global Constraints

- 所有 PG 读取通过 `db_session()`（RLS 自动隔离）
- ClickHouse 查询走 `apihub_core.clickhouse.query_all()` / `query_one()`
- T+1 聚合任务在 quota 服务 `extra_lifespan` 中注册
- `ruff check` + `mypy` clean before commit
- Portal 前端复用现有 `api/client.ts`（JWT auth, Bearer token）
- 新 SQL 追加到 `scripts/init-db/05-billing.sql`
- `billing_record` / `subscription` / `plan` 均在 05-billing.sql 中设置 RLS

---

### Task 1: 数据库 — plan 表 + seed + subscription/billing_record 启用

**Files:**
- Create: `scripts/init-db/05-billing.sql`

- [ ] **Step 1: Create `05-billing.sql`**

```sql
-- Phase 3 计费 schema —— plan 表 + subscription/billing_record 启用

CREATE TABLE IF NOT EXISTS plan (
    code            VARCHAR(32) PRIMARY KEY,
    name            VARCHAR(64) NOT NULL,
    description     TEXT,
    price_cents     BIGINT NOT NULL DEFAULT 0,
    quota_included  JSONB NOT NULL,
    rate_limits     JSONB NOT NULL,
    ai_models       JSONB,
    features        JSONB,
    status          VARCHAR(20) NOT NULL DEFAULT 'active',
    sort_order      INT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO plan (code, name, description, price_cents, quota_included, rate_limits, features, sort_order) VALUES
('free',        'Free',        '个人开发者免费计划',   0,       '{"calls_per_day": 1000, "tokens_per_month": 100000}',        '{"second": 10, "minute": 100}',    '{"api_catalog": true, "try_it": true, "sdk": false}',        1),
('starter',     'Starter',     '小团队入门计划',       99900,  '{"calls_per_day": 50000, "tokens_per_month": 5000000}',      '{"second": 100, "minute": 5000}',  '{"api_catalog": true, "try_it": true, "sdk": true}',         2),
('pro',         'Pro',         '中型团队专业计划',     499900, '{"calls_per_day": 500000, "tokens_per_month": 50000000}',    '{"second": 500, "minute": 25000}', '{"api_catalog": true, "try_it": true, "sdk": true}',         3),
('enterprise',  'Enterprise',  '大客户定制计划',       0,      '{"calls_per_day": 999999999, "tokens_per_month": 999999999}','{"second": 5000, "minute": 250000}','{"api_catalog": true, "try_it": true, "sdk": true}',         4);

CREATE TABLE IF NOT EXISTS subscription (
    tenant_id       BIGINT NOT NULL,
    id              BIGSERIAL PRIMARY KEY,
    plan_code       VARCHAR(64) NOT NULL,
    period_start    TIMESTAMPTZ NOT NULL,
    period_end      TIMESTAMPTZ NOT NULL,
    quota_included  JSONB NOT NULL,
    price_cents     BIGINT NOT NULL,
    status          VARCHAR(20) NOT NULL DEFAULT 'active',
    auto_renew      BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sub_tenant ON subscription(tenant_id, status);

INSERT INTO subscription (tenant_id, plan_code, period_start, period_end, quota_included, price_cents)
SELECT id, 'free', NOW(), '2999-12-31', '{"calls_per_day": 1000, "tokens_per_month": 100000}', 0
FROM tenant
WHERE id NOT IN (SELECT tenant_id FROM subscription WHERE status = 'active');

CREATE TABLE IF NOT EXISTS billing_record (
    tenant_id            BIGINT NOT NULL,
    id                   BIGSERIAL PRIMARY KEY,
    subscription_id      BIGINT REFERENCES subscription(id),
    period_start         TIMESTAMPTZ NOT NULL,
    period_end           TIMESTAMPTZ NOT NULL,
    call_count           BIGINT NOT NULL,
    token_count          BIGINT NOT NULL,
    base_charge_cents    BIGINT NOT NULL DEFAULT 0,
    overage_charge_cents BIGINT NOT NULL DEFAULT 0,
    total_charge_cents   BIGINT NOT NULL DEFAULT 0,
    status               VARCHAR(20) NOT NULL DEFAULT 'pending',
    invoice_url          VARCHAR(512),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_billing_tenant_period ON billing_record(tenant_id, period_start DESC);

ALTER TABLE plan ENABLE ROW LEVEL SECURITY;
ALTER TABLE plan FORCE ROW LEVEL SECURITY;
ALTER TABLE subscription ENABLE ROW LEVEL SECURITY;
ALTER TABLE subscription FORCE ROW LEVEL SECURITY;
ALTER TABLE billing_record ENABLE ROW LEVEL SECURITY;
ALTER TABLE billing_record FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_select ON plan;
CREATE POLICY tenant_isolation_select ON plan FOR SELECT USING (true);
DROP POLICY IF EXISTS tenant_isolation_modify ON plan;
CREATE POLICY tenant_isolation_modify ON plan FOR ALL USING (rls_is_platform_admin());

DROP POLICY IF EXISTS tenant_isolation_select ON subscription;
CREATE POLICY tenant_isolation_select ON subscription FOR SELECT USING (tenant_id = rls_tenant_filter() OR rls_is_platform_admin());
DROP POLICY IF EXISTS tenant_isolation_modify ON subscription;
CREATE POLICY tenant_isolation_modify ON subscription FOR ALL USING (rls_is_platform_admin());

DROP POLICY IF EXISTS tenant_isolation_select ON billing_record;
CREATE POLICY tenant_isolation_select ON billing_record FOR SELECT USING (tenant_id = rls_tenant_filter() OR rls_is_platform_admin());
DROP POLICY IF EXISTS tenant_isolation_modify ON billing_record;
CREATE POLICY tenant_isolation_modify ON billing_record FOR ALL USING (rls_is_platform_admin());

GRANT SELECT, INSERT, UPDATE ON plan, subscription, billing_record TO apihub_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO apihub_app;
```

- [ ] **Step 2: Commit**

```bash
git add scripts/init-db/05-billing.sql
git commit -m "feat(db): plan + subscription + billing_record 表（Phase 3 计费 schema）"
```

---

### Task 2: quota 服务 — models + repository（CH 聚合 + plan/subscription 查询）

**Files:**
- Modify: `services/services/quota/src/quota/models.py`
- Modify: `services/services/quota/src/quota/repository.py`

- [ ] **Step 1: Add billing models to `quota/models.py`**

```python
class PlanSummary(BaseModel):
    code: str
    name: str
    price_cents: int
    quota_included: dict
    features: dict

class DailyApiUsage(BaseModel):
    api_id: str
    api_name: str = ""
    day: str
    calls: int
    tokens: int
    latency_ms: int

class BillingResponse(BaseModel):
    tenant_id: str
    month: str
    plan: PlanSummary
    daily_usage: list[DailyApiUsage]
    total_calls: int
    total_tokens: int
    remaining_calls_today: int
    overage_cents: int = 0
```

- [ ] **Step 2: Add repository functions to `quota/repository.py`**

```python
from apihub_core.clickhouse import query_all
from quota.models import PlanSummary, DailyApiUsage, BillingResponse


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
        "SELECT api_id, toDate(ts) AS day, count() AS calls, sum(token_count) AS tokens, sum(latency_ms) AS latency_ms"
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
        " WHERE tenant_id = %(tenant_id)s AND toYYYYMM(ts) = %(ym)s AND toDayOfMonth(ts) = %(day)s",
        params={"tenant_id": int(tenant_id), "ym": ym, "day": day},
        force_tenant_id=None,
    )
    used = rows[0]["calls"] if rows else 0
    sub = await get_active_subscription(tenant_id)
    plan = await get_plan(sub["plan_code"]) if sub else None
    limit = (plan.quota_included.get("calls_per_day") or 0) if plan else 0
    return max(0, limit - used)
```

- [ ] **Step 3: Verify import**

```bash
.venv-t1/bin/python -c "from quota.models import BillingResponse, PlanSummary; print('OK')"
.venv-t1/bin/python -c "from quota.repository import get_plan, list_plans, get_billing_from_ch; print('OK')"
```

- [ ] **Step 4: Commit**

```bash
git add services/services/quota/src/quota/models.py services/services/quota/src/quota/repository.py
git commit -m "feat(quota): billing models + CH 聚合 repository"
```

---

### Task 3: quota 服务 — routes（/billing + /plans）

**Files:**
- Modify: `services/services/quota/src/quota/routes.py`

- [ ] **Step 1: Add billing and plans routes**

Add imports (update existing `from quota.models import ...` to include `BillingResponse, DailyApiUsage, PlanSummary`).

Replace the placeholder billing endpoint and add plans:

```python
    @app.get("/v1/quota/billing", response_model=BillingResponse)
    async def billing(tenant_id: str, month: str):
        ctx = require_tenant()
        if ctx.tenant_id != tenant_id and not ctx.is_platform_admin:
            raise ApiError(ErrorCode.FORBIDDEN, "cannot view other tenant's billing")
        sub = await repository.get_active_subscription(tenant_id)
        plan = await repository.get_plan(sub["plan_code"]) if sub else None
        ch_rows = await repository.get_billing_from_ch(tenant_id, month)
        remaining = await repository.get_remaining_calls_today(tenant_id)
        daily_usage = [
            DailyApiUsage(api_id=r["api_id"], day=str(r["day"]),
                          calls=r["calls"], tokens=r["tokens"], latency_ms=r["latency_ms"])
            for r in ch_rows
        ]
        return BillingResponse(
            tenant_id=tenant_id, month=month,
            plan=plan or PlanSummary(code="free", name="Free", price_cents=0, quota_included={}, features={}),
            daily_usage=daily_usage,
            total_calls=sum(u.calls for u in daily_usage),
            total_tokens=sum(u.tokens for u in daily_usage),
            remaining_calls_today=remaining,
        )

    @app.get("/v1/quota/plans")
    async def plans():
        return await repository.list_plans()
```

- [ ] **Step 2: Verify import**

```bash
.venv-t1/bin/python -c "from quota.routes import register_routes; print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add services/services/quota/src/quota/routes.py
git commit -m "feat(quota): /v1/quota/billing 真实现 + /v1/quota/plans"
```

---

### Task 4: quota 服务 — T+1 聚合任务

**Files:**
- Modify: `services/services/quota/src/quota/main.py`

- [ ] **Step 1: Rewrite main.py with lifespan task**

```python
"""quota 启动入口 —— 含 T+1 计费聚合。"""

import asyncio
from datetime import datetime, timedelta

from apihub_core import create_app, db
from apihub_core.clickhouse import query_all

from quota.routes import register_routes


async def _daily_billing_aggregation(settings):
    while True:
        now = datetime.utcnow()
        target = now.replace(hour=2, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())

        yesterday = target - timedelta(days=1)
        month_start = yesterday.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        month_end = (month_start + timedelta(days=32)).replace(day=1) - timedelta(seconds=1)

        rows = query_all(
            "SELECT tenant_id, count() AS call_count, sum(token_count) AS token_count"
            " FROM api_call_log"
            " WHERE ts >= %(start)s AND ts < %(end)s"
            " GROUP BY tenant_id",
            params={"start": yesterday.replace(hour=0, minute=0, second=0),
                    "end": yesterday.replace(hour=23, minute=59, second=59)},
            force_tenant_id=None,
        )
        async with db.db_session() as conn:
            for row in rows:
                sub = await conn.fetchrow(
                    "SELECT id FROM subscription WHERE tenant_id = $1 AND status = 'active' LIMIT 1",
                    row["tenant_id"],
                )
                if not sub:
                    continue
                exists = await conn.fetchval(
                    "SELECT 1 FROM billing_record WHERE tenant_id = $1 AND period_start = $2",
                    row["tenant_id"], month_start,
                )
                if exists:
                    continue
                await conn.execute(
                    "INSERT INTO billing_record"
                    " (tenant_id, subscription_id, period_start, period_end,"
                    "  call_count, token_count, base_charge_cents, overage_charge_cents,"
                    "  total_charge_cents, status)"
                    " VALUES ($1,$2,$3,$4,$5,$6,0,0,0,'pending')",
                    row["tenant_id"], sub["id"], month_start, month_end,
                    row["call_count"], row["token_count"],
                )


def build_app():
    return create_app(
        service_name="quota",
        build_routes=register_routes,
        skip_auth_paths=(
            "/health", "/metrics", "/v1/quota/health",
            "/v1/quota/check", "/v1/quota/check-strict", "/v1/quota/refund",
            "/docs", "/openapi.json",
        ),
        extra_lifespan=_daily_billing_aggregation,
    )


app = build_app()
```

- [ ] **Step 2: Verify**

```bash
.venv-t1/bin/python -c "from quota.main import app; print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add services/services/quota/src/quota/main.py
git commit -m "feat(quota): T+1 计费聚合任务（每日 02:00）"
```

---

### Task 5: portal-bff — models + repository（billing/plan/subscription）

**Files:**
- Modify: `services/services/portal/src/portal/models.py`
- Modify: `services/services/portal/src/portal/repository.py`

- [ ] **Step 1: Add PlanInfo and SubscriptionInfo models**

Append to `portal/models.py`:

```python
class PlanInfo(BaseModel):
    code: str
    name: str
    description: str | None = None
    price_cents: int = 0
    quota_included: dict = {}
    rate_limits: dict = {}
    features: dict | None = None
    sort_order: int = 0

class SubscriptionInfo(BaseModel):
    plan_code: str
    plan_name: str = ""
    period_start: str = ""
    period_end: str = ""
    status: str = ""
    auto_renew: bool = True
```

- [ ] **Step 2: Add repository functions**

Append to `portal/repository.py`:

```python
from portal.models import PlanInfo, SubscriptionInfo


async def get_billing_summary(tenant_id: str) -> dict:
    import httpx
    from apihub_core.config import get_settings
    settings = get_settings()
    month = __import__('datetime').datetime.utcnow().strftime("%Y-%m")
    quota_url = getattr(settings, "quota_service_url", "http://quota.apihub-system/v1/quota/billing")
    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.get(quota_url, params={"tenant_id": tenant_id, "month": month})
    if r.status_code != 200:
        return {"tenant_id": tenant_id, "month": month, "plan": {}, "daily_usage": [],
                "total_calls": 0, "total_tokens": 0, "remaining_calls_today": 0}
    return r.json()


async def list_plans() -> list[PlanInfo]:
    async with db.db_session() as conn:
        rows = await conn.fetch("SELECT * FROM plan WHERE status = 'active' ORDER BY sort_order")
    return [PlanInfo(code=r["code"], name=r["name"], description=r.get("description"),
                     price_cents=r["price_cents"], quota_included=r["quota_included"] or {},
                     rate_limits=r["rate_limits"] or {}, features=r.get("features"),
                     sort_order=r["sort_order"]) for r in rows]


async def get_subscription(tenant_id: str) -> SubscriptionInfo | None:
    async with db.db_session() as conn:
        row = await conn.fetchrow("SELECT s.*, p.name AS plan_name FROM subscription s JOIN plan p ON p.code = s.plan_code WHERE s.tenant_id = $1 AND s.status = 'active' LIMIT 1", tenant_id)
    if not row: return None
    return SubscriptionInfo(plan_code=row["plan_code"], plan_name=row["plan_name"],
                            period_start=row["period_start"].isoformat(), period_end=row["period_end"].isoformat(),
                            status=row["status"], auto_renew=row["auto_renew"])
```

- [ ] **Step 3: Verify**

```bash
.venv-t1/bin/python -c "from portal.models import PlanInfo, SubscriptionInfo; print('OK')"
.venv-t1/bin/python -c "from portal.repository import list_plans, get_subscription; print('OK')"
```

- [ ] **Step 4: Commit**

```bash
git add services/services/portal/src/portal/models.py services/services/portal/src/portal/repository.py
git commit -m "feat(portal): billing/plan/subscription 模型+数据访问"
```

---

### Task 6: portal-bff — routes（3 个新端点）

**Files:**
- Modify: `services/services/portal/src/portal/routes.py`

- [ ] **Step 1: Add routes after try endpoint**

Add import:
```python
from portal.models import ApiKeyCreate, ApiKeyResponse, AppCreate, AppResponse, TryRequest, PlanInfo, SubscriptionInfo
```

Add before app/key section:
```python
    # ========== 用量/计费（需 JWT）==========
    @app.get("/v1/portal/usage")
    async def portal_usage():
        ctx = require_tenant()
        return await repository.get_billing_summary(ctx.tenant_id)

    @app.get("/v1/portal/plans", response_model=list[PlanInfo])
    async def portal_plans():
        require_tenant()
        return await repository.list_plans()

    @app.get("/v1/portal/subscription")
    async def portal_subscription():
        ctx = require_tenant()
        sub = await repository.get_subscription(ctx.tenant_id)
        return sub if sub else {"plan_code": "free", "plan_name": "Free", "status": "active"}
```

- [ ] **Step 2: Verify + Commit**

```bash
.venv-t1/bin/python -c "from portal.routes import register_routes; print('OK')"
git add services/services/portal/src/portal/routes.py
git commit -m "feat(portal-routes): /v1/portal/usage + /plans + /subscription"
```

---

### Task 7: portal-bff 单测

**Files:**
- Modify: `services/services/portal/tests/test_routes.py`

- [ ] **Step 1: Add 3 test functions**

```python
async def test_portal_plans(client, monkeypatch):
    from portal.models import PlanInfo
    async def fake_plans():
        return [PlanInfo(code="free", name="Free", price_cents=0, quota_included={}, rate_limits={}, sort_order=1)]
    monkeypatch.setattr("portal.routes.repository.list_plans", fake_plans)
    r = await client.get("/v1/portal/plans")
    assert r.status_code == 200
    assert r.json()[0]["code"] == "free"


async def test_portal_subscription(client, monkeypatch):
    from portal.models import SubscriptionInfo
    async def fake_sub(tenant_id):
        return SubscriptionInfo(plan_code="free", plan_name="Free", period_start="2026-01-01",
                                period_end="2999-12-31", status="active", auto_renew=True)
    monkeypatch.setattr("portal.routes.repository.get_subscription", fake_sub)
    r = await client.get("/v1/portal/subscription")
    assert r.status_code == 200
    assert r.json()["plan_code"] == "free"


async def test_portal_usage(client, monkeypatch):
    async def fake_usage(tenant_id):
        return {"tenant_id": tenant_id, "month": "2026-07", "plan": {"code": "free"},
                "daily_usage": [], "total_calls": 0, "total_tokens": 0, "remaining_calls_today": 1000}
    monkeypatch.setattr("portal.routes.repository.get_billing_summary", fake_usage)
    r = await client.get("/v1/portal/usage")
    assert r.status_code == 200
    assert r.json()["remaining_calls_today"] == 1000
```

- [ ] **Step 2: Run tests**

```bash
.venv-t1/bin/python -m pytest services/services/portal/tests/test_routes.py -v
```

- [ ] **Step 3: Commit**

```bash
git add services/services/portal/tests/test_routes.py
git commit -m "test(portal): billing/plan/subscription 路由单测"
```

---

### Task 8: Portal 前端 — Usage 页

**Files:**
- Create: `frontend/portal/src/pages/Usage.tsx`

- [ ] **Step 1: Create Usage.tsx** (see spec for complete component code — 3 sections: 本月概况, Plan 对比, API 明细)

- [ ] **Step 2: Verify TypeScript**

```bash
cd /home/applo/project/ai-system-arch/frontend/portal && npx tsc --noEmit 2>&1 | head -10
```

- [ ] **Step 3: Commit**

```bash
git add frontend/portal/src/pages/Usage.tsx
git commit -m "feat(portal-frontend): 用量看板页"
```

---

### Task 9: Portal 前端 — 路由注册

**Files:**
- Modify: `frontend/portal/src/App.tsx`

- [ ] **Step 1: Add Usage import and route**

```typescript
import { Usage } from './pages/Usage';
// ...
<Route path="/usage" element={auth ? <Usage /> : <Navigate to="/login" />} />
```

- [ ] **Step 2: Verify TS + Commit**

```bash
cd /home/applo/project/ai-system-arch/frontend/portal && npx tsc --noEmit
git add frontend/portal/src/App.tsx
git commit -m "feat(portal-frontend): /usage 路由"
```

---

### Task 10: 端到端 smoke 扩展

**Files:**
- Modify: `scripts/smoke/portal-onboarding.py`

- [ ] **Step 1: Append step ⑨**

After step ⑧, add:
```python
    print("== ⑨ 查用量统计 ==")
    st, body = http("GET", f"{PORTAL_URL}/v1/portal/usage", headers=auth_hdr)
    usage = json.loads(body)
    print(f"  usage -> HTTP {st}, plan={usage.get('plan', {}).get('code')}, calls={usage.get('total_calls')}")
    assert st == 200
    assert usage.get("plan", {}).get("code") in ("free", "starter", "pro", "enterprise")
```

- [ ] **Step 2: Commit**

```bash
git add scripts/smoke/portal-onboarding.py
git commit -m "test(smoke): 用量统计 step ⑨"
```

---

### Task 11: lint + final checks

- [ ] **Step 1: Run all checks**

```bash
.venv-t1/bin/ruff check services/services/quota/ services/services/portal/
.venv-t1/bin/mypy services/services/quota/ services/services/portal/
.venv-t1/bin/python -m pytest services/services/portal/tests/test_routes.py -v
```

- [ ] **Step 2: Commit fixes if any**

```bash
git add -A && git commit -m "chore: lint fixes"
```
