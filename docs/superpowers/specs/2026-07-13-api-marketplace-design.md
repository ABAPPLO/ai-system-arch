# Phase 4「API 市场化」设计

> 日期：2026-07-13
> 阶段：Phase 4 演进 — API 市场化（计费/套餐/对账）
> 关联：ADR-002（内免外收）、docs/04-data-model.md、docs/03-services.md

## 1. Goal

在已有 plan/subscription/billing_record 三张表 + Portal 用量看板基础上，构建开发者自助计费全链路：Plan 对比/升降级、用量配额可视化、周期性出账、账单记录、Admin 后台计费管理。

### 1.1 已有基础

| 组件 | 状态 | 说明 |
|------|------|------|
| `plan` 表 | ✅ | 4 个 plan（free/starter/pro/enterprise），含 quota/rate-limit/features |
| `subscription` 表 | ✅ | tenant 订阅关系，含周期、自动续期 |
| `billing_record` 表 | ✅ | 月度账单记录 |
| CH `api_call_log` | ✅ | 含 token_prompt/completion/total，可按 tenant+月份聚合 |
| Portal `/usage` 页 | ✅ | 用量看板 + plan table + ProgressBar |
| Portal BFF 端点 | ✅ | `GET /v1/portal/usage`、`/plans`、`/subscription` |

### 1.2 本切片做

- **billing-svc 新服务**：出账 Job（查 CH 算超额）+ 账单查询 + Admin 计费管理
- **portal-bff 扩展**：Plan 升级/降级、账单列表
- **Portal 前端**：Plan 对比页（`/plans`）、账单历史页（`/invoices`）、用量页增强
- **Admin 前端**：计费管理页（租户账单全览、手动出账/调整/切换 Plan）
- **DB 增强**：plan 加超额定价、billing_record 加详情、billing_job_log 新表

### 1.3 非目标

- 真实支付网关对接（账单金额仅供内部对账，收款走线下合同）
- 自动扣款/退款（Admin 手动操作）
- 实时用量告警（后续切片）
- 多币种/税务处理
- Plan 定制化配置页面（Enterprise 走线下）

### 1.4 成功标准

- ✅ `POST /v1/billing/periodic` dry_run → 返回正确的超额计算预览
- ✅ `POST /v1/billing/periodic` → 写入 billing_record + 滚动 subscription 周期
- ✅ Portal `/plans` 页 → 展示 4 个 plan 卡片 + 功能对比表
- ✅ Portal `/invoices` 页 → 展示该租户的历史账单列表
- ✅ Portal `/usage` 页 → 用量超阈值预警 + Plan 信息卡片
- ✅ Admin 计费页 → 租户账单全览 + 手动调整/出账/切 Plan
- ✅ `ruff check` + `mypy` clean

## 2. 架构总览

```
┌─ Portal 前端 ───────────────────┐  ┌─ Admin 前端 ────────────┐
│  /usage       → 用量看板（增强）  │  │  /admin/billing         │
│  /plans       → Plan 对比/升级   │  │  → 租户账单全览         │
│  /invoices    → 账单历史         │  │  → 手动出账/调整/切Plan │
└──────────┬────────────────────┘  └──────┬─────────────────┘
           │ JWT                           │ Admin API Key
           ▼                               ▼
┌──────────────────────────┐  ┌──────────────────────────┐
│  portal-bff (:8011)       │  │  admin-bff (:8006)        │
│  + /v1/portal/subscribe  │  │  + /v1/admin/billing/*     │
│  + /v1/portal/invoices   │  │  + /v1/admin/subscription/*│
└──────────┬────────────────┘  └──────┬──────────────────┘
           │ HTTP                       │ HTTP
           ▼                            ▼
┌──────────────────────────────────────────────────────────┐
│              billing-svc (:8014)                         │
│                                                          │
│  出账 & 计费 核心逻辑                                      │
│                                                          │
│  POST /v1/billing/periodic     — 出账 Job                │
│  GET  /v1/billing/records      — 租户账单列表             │
│  GET  /v1/admin/billing/summary— Admin 汇总               │
│  POST /v1/admin/billing/adjust — 手动调整                 │
│  POST /v1/admin/subscription/override — 切换 Plan         │
└──────────────────┬───────────────────────────────────────┘
                   │ admin_db_session()   │ CH query()
                   ▼                      ▼
            ┌──────────────┐    ┌──────────────────┐
            │  PostgreSQL   │    │  ClickHouse       │
            │ subscription  │    │  api_call_log     │
            │ billing_record│    │  (用量数据)       │
            │ plan          │    └──────────────────┘
            │ billing_job_log│
            └──────────────┘
```

### 2.1 出账数据流

```
每月出账（手动或定时触发）
→ billing-svc POST /v1/billing/periodic

对每个 active subscription:
  1. 读 subscription.quota_included
  2. 查 CH:
     SELECT sum(is_success) as calls, sum(token_total) as tokens
     FROM api_call_log
     WHERE tenant_id = $1 AND ts >= $2 AND ts < $3
  3. 算超额:
     overage_calls = max(0, actual_calls - quota_calls)
     overage_tokens = max(0, actual_tokens - quota_tokens)
  4. 计价:
     overage_charge = overage_calls/1000 * unit_price_calls
                    + overage_tokens/100000 * unit_price_tokens
  5. INSERT billing_record (period, base=plan.price_cents, overage)
  6. UPDATE subscription 滚动到下周期

写 billing_job_log（job_id, tenant_count, total 等）
```

## 3. 数据模型变更

### 3.1 plan 加超额定价

```sql
ALTER TABLE plan ADD COLUMN IF NOT EXISTS overage_unit_price jsonb;
```

示例值：

```json
{"calls_per_1000": 5, "tokens_per_100000": 10}
```

含义：每超 1000 次调用收 ¥0.05（5 cents），每超 10 万 token 收 ¥0.10（10 cents）。单位统一为 cents。

### 3.2 billing_record 增强

```sql
ALTER TABLE billing_record ADD COLUMN IF NOT EXISTS details jsonb;
ALTER TABLE billing_record ADD COLUMN IF NOT EXISTS period TEXT;
```

`details` 示例：
```json
{
  "total_calls": 325000,
  "total_tokens": 28000000,
  "overage_calls": 25000,
  "overage_tokens": 3000000,
  "unit_price_calls": 5,
  "unit_price_tokens": 10,
  "plan_code": "pro",
  "period_start": "2026-07-01",
  "period_end": "2026-07-31"
}
```

### 3.3 新增 billing_job_log

```sql
CREATE TABLE IF NOT EXISTS billing_job_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    period          TEXT NOT NULL,
    tenant_count    INT NOT NULL DEFAULT 0,
    total_base      BIGINT NOT NULL DEFAULT 0,
    total_overage   BIGINT NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'running',
    error_msg       TEXT,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at     TIMESTAMPTZ
);
```

## 4. billing-svc 端点

### 4.1 `POST /v1/billing/periodic` — 出账 Job

```python
@app.post("/v1/billing/periodic")
async def run_billing(
    period: str = "",
    dry_run: bool = False,
    tenant_ids: list[str] | None = None,
):
```

**响应：**

```json
{
  "job_id": "j_xxx",
  "period": "2026-07",
  "total_tenants": 15,
  "total_base_cents": 7498500,
  "total_overage_cents": 3500,
  "records": [
    {
      "tenant_id": "t_001",
      "plan_code": "pro", "plan_name": "Pro",
      "total_calls": 325000, "total_tokens": 28000000,
      "quota_calls": 500000, "quota_tokens": 50000000,
      "overage_calls": 0, "overage_tokens": 0,
      "base_cents": 499900, "overage_cents": 0
    }
  ]
}
```

**dry_run=true：** 只返回预览数据，不写 PG / 不滚动 subscription。

### 4.2 `GET /v1/billing/records` — 租户账单列表

```python
@app.get("/v1/billing/records")
async def get_billing_records(limit: int = 12, offset: int = 0):
    """当前租户的账单历史（RLS 自动隔离）。"""
```

`{items: [{id, period, plan_name, total_calls, total_tokens, base_cents, overage_cents, total_cents, status, created_at}], total: N}`

### 4.3 Admin 端点

```python
@admin_router.get("/v1/admin/billing/summary")
async def admin_billing_summary(period: str = "", tenant_search: str = ""):
    """平台所有租户的账单汇总。"""

@admin_router.post("/v1/admin/billing/adjust")
async def admin_adjust_billing(record_id: str, delta_cents: int, reason: str):
    """调整 billing_record（退款/补收）。"""

@admin_router.post("/v1/admin/subscription/override")
async def admin_override_subscription(tenant_id: str, plan_code: str, reason: str):
    """手动切换租户 plan。"""
```

## 5. Portal 前端

### 5.1 `/plans` — Plan 对比与升级

**4 个 Plan 卡片 + 功能对比表：**

| 功能 | Free | Starter | Pro | Enterprise |
|------|------|---------|-----|------------|
| 日调用上限 | 1k | 5w | 50w | 无限 |
| Token/月 | 10w | 500w | 5000w | 无限 |
| API 目录 | ✓ | ✓ | ✓ | ✓ |
| 在线调试 | ✓ | ✓ | ✓ | ✓ |
| SDK 下载 | ✗ | ✓ | ✓ | ✓ |
| 超额定价 | — | ¥5/千次 | ¥3/千次 | 按合同 |
| 月费 | ¥0 | ¥999 | ¥4,999 | 定制定价 |

**交互：**
- 当前 plan 高亮 + "当前" Badge
- 可升级的 plan 显示"立即升级"按钮 → 确认弹窗 → `POST /v1/portal/subscribe`
- Enterprise 显示"联系销售"链接
- 降级：下周期生效，确认弹窗显示变更摘要
- **加载态：** 居中 spinner
- **错误态：** toast "加载失败"
- **空态：** 无 plan 数据

### 5.2 `/invoices` — 账单历史

**表格列：** 周期 | Plan | 调用量 | Token | 基础费 | 超额费 | 合计 | 状态

**状态 Badge：** pending=黄色、invoiced=绿色、adjusted=蓝色

**交互：** 点击行展开详情、空态提示、分页

### 5.3 Usage 页增强

- Plan 信息卡片：当前 plan、周期起止、自动续费
- 用量条超 80% → 橙色、超 100% → 红色 + "超出配额"
- "升级 Plan"快捷按钮

## 6. Admin 前端

### 6.1 计费管理页

**功能：**
- 租户列表 + 当前 plan + 当月用量 + 费用 + 状态
- 搜索租户
- 出账按钮 → `POST /v1/billing/periodic`
- 调整 → 弹窗：金额 + 原因
- 切 Plan → 下拉选择 + 原因
- 周期筛选下拉
- 汇总行：总租户数、总收入
- 导出 CSV

## 7. 文件清单

### 7.1 新服务

```
services/services/billing/
├── pyproject.toml
├── src/billing/
│   ├── __init__.py
│   ├── main.py
│   ├── routes.py         # 6 端点
│   ├── models.py
│   ├── repository.py
│   └── billing_job.py
└── tests/
    ├── __init__.py
    ├── conftest.py
    ├── test_routes.py
    └── test_billing_job.py
```

### 7.2 修改文件（14 个）

| 文件 | 改动 |
|------|------|
| `scripts/init-db/05-billing.sql` | +overage_unit_price / +details / +billing_job_log |
| `services/services/portal/src/portal/routes.py` | +subscribe + invoices |
| `services/services/portal/src/portal/repository.py` | +subscribe_plan + get_invoices |
| `services/services/portal/src/portal/models.py` | +SubscribeRequest + InvoiceInfo |
| `services/services/admin/src/admin/routes.py` | +billing 管理端点 |
| `frontend/portal/src/pages/Plans.tsx` | 新增 |
| `frontend/portal/src/pages/Invoices.tsx` | 新增 |
| `frontend/portal/src/pages/Usage.tsx` | 增强 |
| `frontend/portal/src/App.tsx` | +/plans /invoices |
| `frontend/admin/src/pages/Billing.tsx` | 新增 |
| `frontend/admin/src/App.tsx` | +/admin/billing |
| `Makefile` | +run-billing |
| `services/libs/apihub-core/src/apihub_core/config.py` | +billing_* |

## 8. 实现顺序

| # | 任务 | 产出 |
|---|------|------|
| 1 | DB migration — plan/billing_record 增强 + billing_job_log | 1 SQL |
| 2 | billing-svc models + repository | 2 文件 |
| 3 | billing_job.py — 出账核心（查 CH + 算超额 + 写 PG） | 1 文件 |
| 4 | billing-svc routes + main | 2 文件 |
| 5 | billing-svc tests | 3 文件 |
| 6 | portal-bff 扩展（subscribe + invoices） | 3 文件 |
| 7 | Portal 前端（Plans + Invoices + Usage 增强） | 4 文件 |
| 8 | admin-bff + Admin 前端（Billing 管理） | 3 文件 |
| 9 | Makefile + Dockerfile + config | 3 文件 |
| 10 | lint | — |

## 9. 风险

| 风险 | 影响 | 对策 |
|------|------|------|
| CH 查询流量大（每月遍历全量日志） | 低 | 按 partition 剪枝 + 预聚合表 |
| 多人同时触发 periodic | 中 | 幂等锁（billing_job_log 防重） |
| 超额定价不合理 | 中 | 初始保守定价 + dry_run 预览 |
| 租户用量突增导致大额超额 | 低 | 出账前 admin 可先 dry_run 预览 |
