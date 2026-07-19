-- ============================================================
-- 13-quota-rules-seed.sql — R3a-3layer Task 2: seed 3-layer rate_limit JSONB
--
-- 背景（T1 c896400 后端 status）：
--   - Go LoadRules 已对齐 Python repository.load_rules（app / tenant /
--     api_version.rate_limit JSONB 3 层 merge），弃 defaultRules/quota_rule。
--   - 全空 → QuotaRules{} = unlimited（对齐 Python EMPTY_RULES）。
--   - 因此 Go quota 默认不限流——必须有 seed 才能验证 R3A-E Lua 原子性
--     （burst N，断 admitted == seed second max）。
--
-- 同时补 schema 债（surfaced by this seed）：
--   - Python load_rules / Go LoadRules SQL 都 `SELECT rate_limit FROM app`/
--     `FROM tenant`，但 01-schema.sql 从未给 app/tenant 加这列。
--     历史 Python load_rules 实际查询会 `column "app.rate_limit" does not
--     exist` 抛错（asyncpg 默认 raise）→ Python 侧也没有真规则在跑。
--     Go T1 defensive：QueryRow 错误返回 EMPTY_RULES,"default",nil →
--     unlimited。本脚本 ALTER ADD COLUMN IF NOT EXISTS 修补。
--
-- 幂等：ON CONFLICT (id) DO UPDATE SET rate_limit = EXCLUDED.rate_limit。
-- 重跑安全：rate_limit 列与 seed 行重写为 EXCLUDED 值，无副作用。
--
-- 注意：apply-db 以 owner `apihub`（superuser）执行；RLS 对 superuser 不生效。
--       本脚本不带 BEGIN/COMMIT —— 与 12-fix-jsonb-double-encoding.sql 保持
--       互斥（apply-db 不加 --single-transaction，每条各自原子提交）。
-- ============================================================

-- ===== 1. schema 补：app / tenant 加 rate_limit JSONB 列 =====
-- 与 api_version.rate_limit 同语义：jsonb，可 NULL（NULL = 该层未配规则，
-- load_rules 视为 fall-through 到下一层）。asyncpg / pgx 的 jsonb codec 都
-- 自动把 jsonb object 反序列化为 map（Python）/ []byte（Go，由 parseRulesBlob
-- json.Unmarshal）；NULL 直接给 nil。
ALTER TABLE app    ADD COLUMN IF NOT EXISTS rate_limit jsonb;
ALTER TABLE tenant ADD COLUMN IF NOT EXISTS rate_limit jsonb;

-- ===== 2. tenant 层 seed（演示 source=tenant + Lua 原子性独立计量）=====
-- tenant_smoke_r3a_lua：rate_limit 在 tenant 层（app/api_version 维度对该
-- tenant 不存在行 → 三层 fall-through 到 tenant 层）。second=5 是一个明显
-- 非 defaultRules(10) 的值，e2e R3A-E burst 15 → admitted 精确 5、blocked 10，
-- 证明规则来自 seed（不是历史 hardcoded default）。
INSERT INTO tenant (id, name, slug, type, status, tier, rate_limit) VALUES
    ('tenant_smoke_r3a',     'Smoke R3a',         'smoke-r3a',     'internal', 'active', 'standard', NULL),
    ('tenant_smoke_r3a_lua', 'Smoke R3a (Lua iso)', 'smoke-r3a-lua', 'internal', 'active', 'standard',
     '{"second":{"max_count":5,"window_seconds":1}}'::jsonb)
ON CONFLICT (id) DO UPDATE SET rate_limit = EXCLUDED.rate_limit;

-- ===== 3. app 层 seed（演示 source=app + app > api_version 合并）=====
-- app_smoke_r3a：second=20 覆盖 api_version.second=10，minute/day 由 api_version
-- 提供（100/1000）—— 证明 mergeRules per-tier override 语义。
INSERT INTO app (id, tenant_id, name, type, status, quota_tier, rate_limit) VALUES
    ('app_smoke_r3a', 'tenant_smoke_r3a', 'Smoke R3a App', 'server', 'active', 'standard',
     '{"second":{"max_count":20,"window_seconds":1}}'::jsonb)
ON CONFLICT (id) DO UPDATE SET rate_limit = EXCLUDED.rate_limit;

-- ===== 4. api_version 层 seed（floor：second=10, minute=100, day=1000）=====
-- 历史 R3a defaultRules(10/100/1000) 现作为显式 api_version.rate_limit 落地，
-- 由 LoadRules 加载而非 hardcoded。app override 在 second 上覆盖到 20。
SET app.tenant_id = 'tenant_smoke_r3a';

INSERT INTO api (id, tenant_id, name, base_path, category, status, visibility) VALUES
    ('api_smoke_r3a', 'tenant_smoke_r3a', 'Smoke R3a API', '/smoke-r3a', 'smoke', 'published', 'tenant')
ON CONFLICT (id) DO NOTHING;

INSERT INTO api_version (
    id, tenant_id, api_id, version, backend_type, backend_url, method, path, status, rate_limit
) VALUES
    ('ver_smoke_r3a_v1', 'tenant_smoke_r3a', 'api_smoke_r3a', 'v1', 'http',
     'http://example.internal/v1/smoke', 'GET', '/v1/smoke', 'published',
     '{"second":{"max_count":10,"window_seconds":1},"minute":{"max_count":100,"window_seconds":60},"day":{"max_count":1000,"window_seconds":86400}}'::jsonb)
ON CONFLICT (id) DO UPDATE SET rate_limit = EXCLUDED.rate_limit;

RESET app.tenant_id;
