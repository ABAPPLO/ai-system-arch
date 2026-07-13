-- Phase 3 Webhook —— webhook_subscription 表

CREATE TABLE IF NOT EXISTS webhook_subscription (
    id          text PRIMARY KEY,
    tenant_id   text NOT NULL,
    url         text NOT NULL,
    events      text[] NOT NULL DEFAULT '{}',
    secret      text DEFAULT '',
    status      text NOT NULL DEFAULT 'active',  -- active / paused
    created_at  timestamptz NOT NULL DEFAULT NOW(),
    updated_at  timestamptz NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_webhook_tenant ON webhook_subscription(tenant_id, status);

ALTER TABLE webhook_subscription ENABLE ROW LEVEL SECURITY;
ALTER TABLE webhook_subscription FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_select ON webhook_subscription;
CREATE POLICY tenant_isolation_select ON webhook_subscription
    FOR SELECT USING (tenant_id = rls_tenant_filter() OR rls_is_platform_admin());
DROP POLICY IF EXISTS tenant_isolation_modify ON webhook_subscription;
CREATE POLICY tenant_isolation_modify ON webhook_subscription
    FOR ALL USING (rls_is_platform_admin())
    WITH CHECK (rls_is_platform_admin());

DROP TRIGGER IF EXISTS set_updated_at ON webhook_subscription;
CREATE TRIGGER set_updated_at BEFORE UPDATE ON webhook_subscription
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();

GRANT SELECT, INSERT, UPDATE, DELETE ON webhook_subscription TO apihub_app;
