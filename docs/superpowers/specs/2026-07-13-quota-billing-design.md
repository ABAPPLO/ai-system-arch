# Phase 3 第三切片「配额与计费」设计

> 日期：2026-07-13
> 阶段：Phase 3 开放（`docs/10-roadmap.md` §5）第 3 个子项目
> 关联文档：`docs/04-data-model.md` §2.11（计费预留 schema）、`docs/03-services.md`（quota 服务）

## 1. Goal

让外部开发者能在 Portal 中看到自己当前的 Plan（套餐）、当月 API 调用用量（次数/Token）、以及 Plan 之间的对比，为后续真计费打地基。

### 1.1 范围（本切片做）

- **`plan` 表**（新建）+ seed（free/starter/pro/enterprise）
- **`subscription` + `billing_record` 表**启用（已有 PG 预留 schema）
- **quota 服务扩展**：`/v1/quota/billing` 从 ClickHouse 聚合实现 + T+1 定时聚合任务
- **portal-bff 扩展**：3 个端点（usage/plans/subscription）
- **Portal 前端新增 1 页**：用量看板 `/usage`
- **端到端 smoke 扩展**

### 1.2 非目标（明确 defer）

- 真支付网关对接（Stripe/支付宝）
- Plan 自助切换（走工单审批，本切片只展示）
- 超量自动限流/收费（本切片只展示用量和剩余）
- 发票/对账系统
- 企业实名与计费关联

## 2. 架构总览

```
Portal 前端 (/usage)
  └── portal-bff (8011)
        ├── GET /v1/portal/usage       → 转发 quota
        ├── GET /v1/portal/plans       → plan 表
        └── GET /v1/portal/subscription → subscription 表
              │
        quota 服务 (8004)
              ├── GET /v1/quota/billing → ClickHouse 聚合
              └── T+1 task             → CH → billing_record
```

## 3. 数据模型

### 3.1 `plan` 表（新建）

```sql
CREATE TABLE plan (
    code            VARCHAR(32) PRIMARY KEY,      -- free/starter/pro/enterprise
    name            VARCHAR(64) NOT NULL,
    description     TEXT,
    price_cents     BIGINT NOT NULL DEFAULT 0,    -- 月费（分），enterprise=0 表示定制
    quota_included  JSONB NOT NULL,               -- {calls_per_day: 10000, tokens_per_month: 1000000}
    rate_limits     JSONB NOT NULL,               -- {second: 100, minute: 5000}
    ai_models       JSONB,                        -- 允许的 AI 模型列表（null=不限）
    features        JSONB,                        -- {api_catalog: true, try_it: true, sdk: false}
    status          VARCHAR(20) NOT NULL DEFAULT 'active',
    sort_order      INT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE plan IS '套餐定义 —— 由平台管理员维护，外部开发者只读';

-- Seed
INSERT INTO plan (code, name, description, price_cents, quota_included, rate_limits, features, sort_order) VALUES
('free',        'Free',        '个人开发者免费计划',    0,       '{"calls_per_day": 1000, "tokens_per_month": 100000}',        '{"second": 10, "minute": 100}',    '{"api_catalog": true, "try_it": true, "sdk": false}',        1),
('starter',     'Starter',     '小团队入门计划',        99900,  '{"calls_per_day": 50000, "tokens_per_month": 5000000}',      '{"second": 100, "minute": 5000}',  '{"api_catalog": true, "try_it": true, "sdk": true}',         2),
('pro',         'Pro',         '中型团队专业计划',      499900, '{"calls_per_day": 500000, "tokens_per_month": 50000000}',    '{"second": 500, "minute": 25000}', '{"api_catalog": true, "try_it": true, "sdk": true}',         3),
('enterprise',  'Enterprise',  '大客户定制计划',        0,      '{"calls_per_day": 999999999, "tokens_per_month": 999999999}','{"second": 5000, "minute": 250000}','{"api_catalog": true, "try_it": true, "sdk": true}',         4);
```

### 3.2 `subscription` 表（启用已有预留表）

```sql
CREATE TABLE subscription (
    tenant_id       BIGINT NOT NULL,
    id              BIGSERIAL PRIMARY KEY,
    plan_code       VARCHAR(64) NOT NULL,
    period_start    TIMESTAMPTZ NOT NULL,
    period_end      TIMESTAMPTZ NOT NULL,
    quota_included  JSONB NOT NULL,               -- 套餐内含配额（快照）
    price_cents     BIGINT NOT NULL,
    status          VARCHAR(20) NOT NULL DEFAULT 'active',
    auto_renew      BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 新租户默认 subscription
INSERT INTO subscription (tenant_id, plan_code, period_start, period_end, quota_included, price_cents)
SELECT id, 'free', NOW(), '2999-12-31', '{"calls_per_day": 1000, "tokens_per_month": 100000}', 0
FROM tenant;
```

### 3.3 `billing_record` 表（启用已有预留表）

```sql
CREATE TABLE billing_record (
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

CREATE INDEX idx_billing_tenant_period ON billing_record(tenant_id, period_start DESC);
```

## 4. quota 服务扩展

### 4.1 `GET /v1/quota/billing` — 用量聚合

请求：`GET /v1/quota/billing?tenant_id=xxx&month=2026-07`

响应：

```python
class PlanSummary(BaseModel):
    code: str
    name: str
    price_cents: int
    quota_included: dict    # {calls_per_day, tokens_per_month}
    features: dict

class DailyApiUsage(BaseModel):
    api_id: str
    api_name: str
    day: str                # "2026-07-01"
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
    remaining_calls_today: int          # 今日剩余
    overage_cents: int = 0              # 超量费用（预留）
```

**实现**：从 ClickHouse `api_call_log` 按 tenant_id + 月份聚合：

```sql
SELECT api_id,
       toDate(ts) AS day,
       count() AS calls,
       sum(token_count) AS tokens,
       sum(latency_ms) AS latency_ms
FROM api_call_log
WHERE tenant_id = %(tenant_id)s
  AND toYYYYMM(ts) = %(month)s
GROUP BY api_id, day
ORDER BY day, api_id
```

同时查 `subscription` 拿 plan 信息和 `quota_included` 快照；今日剩余从 Redis（quota 限流器已有 `GET /v1/quota/usage`）。

### 4.2 T+1 聚合任务（每日 02:00）

在 quota 服务的 `extra_lifespan` 中注册一个后台定时任务：

```python
async def _daily_aggregation(settings):
    """每日 02:00 将前一天的 api_call_log 聚合成 billing_record。"""
    while True:
        now = datetime.utcnow()
        target = now.replace(hour=2, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())

        prev_month_start = (target - timedelta(days=1)).replace(day=1)
        prev_month_end = (prev_month_start + timedelta(days=32)).replace(day=1) - timedelta(seconds=1)

        # CH 聚合所有 tenant
        rows = query_all("""
            SELECT tenant_id,
                   count() AS call_count,
                   sum(token_count) AS token_count
            FROM api_call_log
            WHERE ts >= %(start)s AND ts < %(end)s
            GROUP BY tenant_id
        """, params={"start": prev_month_start, "end": prev_month_end})

        async with db.db_session() as conn:
            for row in rows:
                sub = await conn.fetchrow(
                    "SELECT id FROM subscription WHERE tenant_id = $1 AND status = 'active' LIMIT 1",
                    row["tenant_id"],
                )
                if not sub:
                    continue
                await conn.execute("""
                    INSERT INTO billing_record
                        (tenant_id, subscription_id, period_start, period_end,
                         call_count, token_count, base_charge_cents, overage_charge_cents,
                         total_charge_cents, status)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'pending')
                """, row["tenant_id"], sub["id"], prev_month_start, prev_month_end,
                    row["call_count"], row["token_count"], 0, 0, 0)
```

任务幂等：对同一 `tenant_id + period_start` 已存在 `billing_record` 则跳过（`ON CONFLICT` 或 INSERT 前检查）。

### 4.3 quota 路由变更

- `GET /v1/quota/billing` — 占位 → 真实现（CH 聚合 + subscription + plan）
- 新增 `GET /v1/quota/plans` — 返回 plan 列表（portal-bff 转发用）

## 5. portal-bff 扩展

### 5.1 新增端点

| 端点 | 方法 | 说明 | 转发/直写 |
|------|------|------|-----------|
| `GET /v1/portal/usage` | GET | 当月用量 + 剩余 + plan | 转发 quota `/v1/quota/billing` |
| `GET /v1/portal/plans` | GET | Plan 列表（对比） | 直写 `plan` 表 |
| `GET /v1/portal/subscription` | GET | 当前 Plan + 周期 | 直写 `subscription` 表 |

**Pydantic 模型：**

复用 `portal/models.py` 新增：

```python
class PlanInfo(BaseModel):
    code: str
    name: str
    description: str | None
    price_cents: int
    quota_included: dict
    rate_limits: dict
    features: dict | None
    sort_order: int

class SubscriptionInfo(BaseModel):
    plan_code: str
    plan_name: str
    period_start: str
    period_end: str
    status: str
    auto_renew: bool
```

### 5.2 路由

```python
@require_tenant()
@router.get("/v1/portal/usage")
async def usage():
    ctx = require_tenant()
    return await repository.get_billing_summary(ctx.tenant_id)

@require_tenant()
@router.get("/v1/portal/plans")
async def list_plans():
    return await repository.list_plans()

@require_tenant()
@router.get("/v1/portal/subscription")
async def subscription():
    ctx = require_tenant()
    return await repository.get_subscription(ctx.tenant_id)
```

## 6. Portal 前端

### 6.1 新增页面 `/usage`

同现有 Portal 技术栈（React + TS + Vite + Tailwind CSS + Zustand）。

新建 `frontend/portal/src/pages/Usage.tsx`。

**布局：**

```
┌──────────────────────────────────────────────────────────┐
│  < 返回    用量统计                                       │
│                                                          │
│  ┌── 本月概况 ─────────────────────────────────────────┐ │
│  │  月份: 2026-07                                       │ │
│  │                                                      │ │
│  │  ┌─── 调用次数 ───┐  ┌─── Token 消耗 ───┐           │ │
│  │  │ 12,847 / 50k  │  │ 245k / 500 万   │           │ │
│  │  │ ██████░░░░ 25%│  │ ██░░░░░░░░  5% │           │ │
│  │  └────────────────┘  └────────────────┘              │ │
│  │                                                      │ │
│  │  今日剩余: 43,153 次                                  │ │
│  └──────────────────────────────────────────────────────┘ │
│                                                          │
│  ┌── Plan ────────────────────────────────────────────┐  │
│  │  当前: Starter · ¥999/月                            │  │
│  │                                                     │  │
│  │  Free      ¥0      1,000/日     SDK: ✗             │  │
│  │  ▌Starter▐ ¥999    50,000/日    SDK: ✓  当前       │  │
│  │  Pro       ¥4,999  500,000/日   SDK: ✓             │  │
│  │  Enterprise 定制    定制          SDK: ✓            │  │
│  └──────────────────────────────────────────────────────┘ │
│                                                          │
│  ┌── 按 API 明细 ────────────────────────────────────┐  │
│  │  月份: [2026-07 ▼]                                 │  │
│  │                                                     │  │
│  │  API 名           │ 调用  │ Token  │ 占比          │  │
│  │  ├─ 用户查询       │ 5,201 │ 0      │ 40%          │  │
│  │  ├─ LLM 对话       │ 3,847 │ 245k   │ 30%          │  │
│  │  └─ 批量导入       │ 3,799 │ 0      │ 30%          │  │
│  └──────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────┘
```

**组件：**
- 本月概况卡片（调用次数 + Token 消耗 + 进度条 + 今日剩余）
- Plan 对比表格（4 个 plan 横向对比）
- API 明细表格（月份选择 + API 级分组）

**Tab/路由：** 无 Tab——`/usage` 是独立页。侧边导航加"用量统计"链接。

**状态处理：**
- 加载态：整体 spinner
- 空态（无调用数据）："本月尚无 API 调用记录"
- 错误态：toast + 重试按钮
- subscription 不存在：显示 Free plan + "正在为您分配默认套餐"

## 7. 现有文件变更

### 7.1 quota 服务

- `src/quota/routes.py` — `billing` 端点真实现 + 新增 `/v1/quota/plans`
- `src/quota/models.py` — 新增 `PlanSummary`, `BillingResponse`, `DailyApiUsage`
- `src/quota/repository.py` — 新增 `get_active_subscription()`, `get_plan()`, `list_plans()`, `get_billing_from_ch()`
- `src/quota/main.py` — 注册 `_daily_aggregation` 任务（extra_lifespan）

### 7.2 portal-bff

- `src/portal/routes.py` — 新增 3 个端点
- `src/portal/models.py` — 新增 `PlanInfo`, `SubscriptionInfo`
- `src/portal/repository.py` — 新增 `get_billing_summary()`, `list_plans()`, `get_subscription()`
- `tests/test_routes.py` — 新增测试

### 7.3 数据库

- 新建 `02-phase3.sql`（包含 `plan` 表 + seed + `subscription` seed + `billing_record` 索引）

### 7.4 Portal 前端

- `frontend/portal/src/pages/Usage.tsx` — 新增
- `frontend/portal/src/App.tsx` — 注册 `/usage` 路由 + 导航链接

### 7.5 端到端 smoke

- `scripts/smoke/portal-onboarding.py` — 追加步骤 ⑨ 查用量

## 8. Done 标准

- ✅ `plan` 表 + seed 数据（4 个 plan）
- ✅ `subscription` 表启用 + 默认 free plan seed
- ✅ `billing_record` 表启用 + 索引
- ✅ `GET /v1/quota/billing` — CH 聚合返回 month/plan/usage
- ✅ `GET /v1/quota/plans` — 返回 plan 列表
- ✅ T+1 每日聚合任务
- ✅ `GET /v1/portal/usage` — Portal 用量概览
- ✅ `GET /v1/portal/plans` — Plan 列表
- ✅ `GET /v1/portal/subscription` — 当前 Plan 信息
- ✅ Portal 前端 `/usage` 页 — 本月概况 + Plan 对比 + API 明细
- ✅ `ruff check` + `mypy` clean
- ✅ 端到端 smoke：查用量 → 有数据

## 9. 实现顺序

1. `02-phase3.sql` — plan 表 + seed + subscription/billing_record 启用
2. quota `models.py` + `repository.py` — CH 聚合 + subscription/plan 查询
3. quota `routes.py` — `/v1/quota/billing` + `/v1/quota/plans`
4. quota `main.py` — T+1 聚合任务
5. portal-bff `models.py` + `repository.py` — billing/plan/subscription 查询
6. portal-bff `routes.py` — 3 个新端点
7. portal-bff `tests/test_routes.py`
8. Portal 前端 `Usage.tsx` — 用量看板页
9. Portal 前端 `App.tsx` — 路由注册 + 导航
10. 端到端 smoke 扩展
11. `ruff check` + `mypy`
