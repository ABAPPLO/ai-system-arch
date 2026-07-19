# Go quota 3 层 merge + seed（design）

**日期**：2026-07-19
**分支**：`fix/go-quota-3layer-merge`（base main = `bca134a`，R3a 合后）
**来源**：R3a（PR#59）concern-a/b defer —— Go quota 规则加载语义未对齐 Python，是 prod cutover 前置。

## 背景

R3a Go quota 契约对齐 Python（响应/key/Lua/鉴权/health/部署 7 点 e2e PASS），但**规则加载语义 defer**：Go `pg.go LoadRules` 查 `quota_rule` 表（live DB 不存在）→ 总 `defaultRules`(10/100/1000 限流)；Python `repository.py load_rules` 查 app/tenant/api_version.rate_limit JSONB 3 层 merge → `EMPTY_RULES`(unlimited)。本轮让 Go 加载真规则（对齐 Python 3 层 merge），prod cutover 前置。

## 差距（探索确认）

- **Python** `load_rules`（`services/services/quota/src/quota/repository.py:75-114`）：SQL 查 `app.rate_limit` / `tenant.rate_limit` / `api_version.rate_limit` 三层 JSONB；`_parse_rules_blob`（`{second:{max_count,window_seconds,enabled}, minute, day}`，兼容 `max`/`count` 简写）；`_merge`（override 优先，每 tier 独立，app>tenant>api_version）；全空 `EMPTY_RULES`(unlimited)；source `{app,tenant,api_version,default}`。
- **Go** `LoadRules`（`services/go/quota/internal/repository/pg.go:34-54`）：查 `quota_rule` 表（`WHERE tenant_id+app_id+api_id`，**不存在**）→ `defaultRules()`(10/100/1000)；source `{api_version,default,fallback}`。

## 修法（对齐 Python）

### 1. Go LoadRules 改 Python 3 层（`repository/pg.go`）
移植 Python `load_rules`：
- SQL：`SELECT (SELECT rate_limit FROM app WHERE id=$1 AND tenant_id=$2) AS app_rl, (SELECT rate_limit FROM tenant WHERE id=$2) AS tenant_rl, (SELECT rate_limit FROM api_version WHERE api_id=$3 AND tenant_id=$2 ORDER BY status='published' DESC, created_at DESC LIMIT 1) AS api_rl`（同 Python）。
- `_parseRulesBlob`（JSONB → `QuotaRules`，兼容 `max_count`/`max`/`count` + `window_seconds` 简写）+ `_merge`（override 优先，每 tier 独立）+ source 逻辑（app>tenant>api_version>default）。
- 全空 → `EMPTY_RULES`（`QuotaRules{}` 所有 tier zero → 不限流）。**弃 `quota_rule` 表 + `defaultRules()`**。

### 2. Go models（`models/types.go`）
- `LimitRule` 加 `Enabled bool`（Python 有；disabled tier 跳过）。
- `QuotaRules` 全 zero = unlimited（limiter `CheckAndConsume` 已跳过 `MaxCount<=0` tier → 全空不限流，对齐 Python EMPTY_RULES）。

### 3. seed（`scripts/init-db/`）
加 `app`/`tenant`/`api_version` 的 `rate_limit` JSONB demo 规则（如 api_version 层 `{"second":{"max_count":10,"window_seconds":1},"minute":{"max_count":100},"day":{"max_count":1000}}`，app 层覆盖 demo），让 Go/Python 加载真规则（非 defaultRules/EMPTY_RULES）。

### 4. 测试
- **Go repository 单测**：对照 Python merge —— 逐层覆盖（app>tenant>api_version）、source 各分支、全空→unlimited、disabled tier 跳过。用 `pgxmock` 或真 PG（`db_pool` 模式）。
- **kind e2e 回归**：Go 加载 seed 规则 → 限流按 seed（非 defaultRules 10/100/1000）；R3a 的 Lua 原子 e2e 改用 seed 规则（second=10 等）保持原子断言。

## 范围 / 非范围

**范围**：Go `repository/pg.go`（LoadRules 重写）+ `models/types.go`（LimitRule.Enabled）+ init-db seed + Go repository 测试 + R3a e2e 适配（seed 规则）。
**非范围**：`defaultRules()` 函数（弃）、`quota_rule` 表（弃，不建）、Python quota（不改）、多 Region splitRatio（R3b）。

## 风险

- **Go JSONB 解析**：pgx 读 rate_limit JSONB 为 `[]byte`/string → `json.Unmarshal`（vs Python asyncpg codec 返 dict）。注意 NULL → unlimited。
- **unlimited 语义**：全空 `QuotaRules` → limiter 跳过所有 tier（MaxCount<=0）→ 不限流。对齐 Python `EMPTY_RULES`。注意：**这改变了 Go 默认行为**（R3a 总 defaultRules 限流 → 现 unlimited），正是 prod cutover 前置要求（对齐 Python）。
- **R3a e2e Lua 原子**：原用 defaultRules(10/100/1000)。改 seed 规则后，e2e seed `second.max_count=10` 保持 `allowed==10` 原子断言。
- **seed 影响 Python**：seed rate_limit 后 Python 也加载真规则（之前 EMPTY_RULES）——Python 行为也变（从 unlimited → seed 规则）。这是预期的（规则系统激活）。
