# Go quota 3 层 merge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`).

**Goal:** Go `LoadRules` 对齐 Python `load_rules`（app/tenant/api_version.rate_limit JSONB 3 层 merge）+ 弃 `quota_rule`/`defaultRules` + seed + 测试 → prod cutover 前置。

**Architecture:** 逐行移植 Python `repository.py:75-114` 到 Go；全空 `QuotaRules` = unlimited（对齐 `EMPTY_RULES`）。

**Spec:** `docs/superpowers/specs/2026-07-19-go-quota-3layer-merge-design.md`

## Global Constraints

- **逐行对齐 Python** `services/services/quota/src/quota/repository.py:75-114`（SQL + _parse_rules_blob + _merge + source）。
- **弃** `quota_rule` 表 + `defaultRules()` + `RuleRow`（quota_rule 列）。
- 全空 `QuotaRules` = unlimited（limiter 跳过 MaxCount<=0 tier，对齐 Python EMPTY_RULES）。
- Go test：`cd services/go/quota && go test ./... && go vet ./...`。
- GateGuard：每文件首次 bash/edit 拦，报 facts retry。
- 每任务 commit。

---

### Task 1: Go LoadRules 3 层 merge + models + 弃 defaultRules（对齐 Python）

**Files:**
- Modify: `services/go/quota/internal/repository/pg.go`（LoadRules 重写 + _parseRulesBlob + _merge + source；弃 RuleRow/quota_rule SQL/defaultRules）
- Modify: `services/go/quota/internal/models/types.go`（LimitRule 加 Enabled；弃 RuleRow）
- Modify: `services/go/quota/internal/limiter/redis.go`（若引用 defaultRules 则清理；MaxCount<=0 + !Enabled 跳过 tier）
- Test: `services/go/quota/internal/repository/pg_test.go`（新）

**Python 基准（repository.py:75-114）**：SQL 查 app.rate_limit / tenant.rate_limit / api_version.rate_limit（三层 JSONB），_parse_rules_blob（{second:{max_count,window_seconds,enabled},...}，兼容 max/count 简写），_merge（override 优先 per-tier），source app>tenant>api_version>default，全空 EMPTY_RULES。

- [ ] **Step 1: models** — `LimitRule` 加 `Enabled bool`；弃 `RuleRow`（quota_rule 列 struct）。
- [ ] **Step 2: pg.go LoadRules 重写** — 移植 Python SQL（app/tenant/api_version rate_limit，参数顺序 app_id,tenant_id,api_id 同 Python）；`_parseRulesBlob([]byte)`（pgx JSONB→json.Unmarshal→QuotaRules，兼容 max_count/max/count + window_seconds 简写 + enabled）+ `_merge(base,override)`（override 优先 per-tier）+ source（app_rl 非空→app，等）；全空→`QuotaRules{}`（unlimited）。弃 `defaultRules()` + `RuleRow`。
- [ ] **Step 3: limiter 兼容** — CheckAndConsume/GetUsage 跳过 `MaxCount<=0 || !Enabled` tier（全空→不限流）。
- [ ] **Step 4: 测试（TDD）** — `pg_test.go`（pgxmock 或真 PG mini）：(a) 仅 api_version 层 → source=api_version + 规则生效；(b) app 覆盖 tenant 覆盖 api_version → source=app + app 值；(c) 全空 → unlimited（所有 tier 跳过）+ source=default；(d) disabled tier 跳过。对照 Python _merge 语义。
- [ ] **Step 5: go test + vet + commit**

---

### Task 2: seed + e2e 适配

**Files:**
- Create: `scripts/init-db/13-quota-rules-seed.sql`（app/tenant/api_version rate_limit JSONB demo）
- Modify: `scripts/smoke/k8s-go-quota.py`（e2e 改用 seed 规则）

- [ ] **Step 1: seed SQL** — `api_version` 层 `rate_limit='{"second":{"max_count":10,"window_seconds":1},"minute":{"max_count":100,"window_seconds":60},"day":{"max_count":1000,"window_seconds":86400}}'::jsonb`（demo api）；`app` 层覆盖 demo（如 second=20，证明 app>api_version merge）；幂等 UPDATE/ON CONFLICT。
- [ ] **Step 2: e2e 适配** — `k8s-go-quota.py` 的 Lua 原子断言改用 seed 规则（second=10 或 app 覆盖值）；R3A-E `allowed_count` 按 seed max。
- [ ] **Step 3: kind e2e** — apply seed（make db-apply）+ 跑 e2e：Go 加载 seed（source=app 或 api_version，非 defaultRules）；限流按 seed；Lua 原子 PASS。
- [ ] **Step 4: commit**

---

## 风险

- **Go JSONB 解析**：pgx 读 rate_limit 为 `[]byte` → `json.Unmarshal`（NULL→unlimited）。注意 pgx Scan 到 `*[]byte` 或 `pgtype.JSONB`。
- **unlimited 改 Go 默认**：R3a 总 defaultRules(10/100/1000) → 现 unlimited（全空）。正是 prod cutover 要（对齐 Python EMPTY_RULES）。
- **seed 影响 Python**：seed 后 Python 也加载真规则（之前 EMPTY_RULES unlimited → 现 seed 限流）。预期（规则系统激活）；Python 测试可能需适配。
- **R3a e2e Lua 原子**：defaultRules 弃后，e2e 须 seed 规则保持 `allowed==10` 断言。
