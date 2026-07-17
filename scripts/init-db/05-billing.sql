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
('enterprise',  'Enterprise',  '大客户定制计划',       0,      '{"calls_per_day": 999999999, "tokens_per_month": 999999999}','{"second": 5000, "minute": 250000}','{"api_catalog": true, "try_it": true, "sdk": true}',         4)
ON CONFLICT DO NOTHING;

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
WHERE id NOT IN (SELECT tenant_id FROM subscription WHERE status = 'active')
ON CONFLICT DO NOTHING;

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

GRANT SELECT, INSERT, UPDATE ON billing_job_log TO apihub_app;
