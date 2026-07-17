-- R2b notification 渠道层：channel_config / template / log
BEGIN;

-- ===== notification_channel_config（per-tenant，租户可读写自己的）=====
CREATE TABLE IF NOT EXISTS notification_channel_config (
    id           text PRIMARY KEY,
    tenant_id    text NOT NULL,
    channel_type text NOT NULL,
    name         text NOT NULL DEFAULT 'default',
    config       jsonb NOT NULL DEFAULT '{}'::jsonb,
    status       text NOT NULL DEFAULT 'active',
    created_at   timestamptz NOT NULL DEFAULT NOW(),
    updated_at   timestamptz NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, channel_type, name)
);
CREATE INDEX IF NOT EXISTS idx_channelcfg_tenant ON notification_channel_config(tenant_id, channel_type, status);
ALTER TABLE notification_channel_config ENABLE ROW LEVEL SECURITY;
ALTER TABLE notification_channel_config FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation_select ON notification_channel_config;
CREATE POLICY tenant_isolation_select ON notification_channel_config
    FOR SELECT USING (tenant_id = rls_tenant_filter() OR rls_is_platform_admin());
DROP POLICY IF EXISTS tenant_isolation_modify ON notification_channel_config;
CREATE POLICY tenant_isolation_modify ON notification_channel_config
    FOR ALL USING (tenant_id = rls_tenant_filter() OR rls_is_platform_admin())
    WITH CHECK (tenant_id = rls_tenant_filter() OR rls_is_platform_admin());
DROP TRIGGER IF EXISTS set_updated_at ON notification_channel_config;
CREATE TRIGGER set_updated_at BEFORE UPDATE ON notification_channel_config
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();

-- ===== notification_template（平台全局参考数据，无 tenant_id / 无 RLS）=====
CREATE TABLE IF NOT EXISTS notification_template (
    code             text NOT NULL,
    channel_type     text NOT NULL,
    locale           text NOT NULL DEFAULT 'zh-CN',
    subject_tpl      text NOT NULL DEFAULT '',
    body_tpl         text NOT NULL,
    variables_schema jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at       timestamptz NOT NULL DEFAULT NOW(),
    PRIMARY KEY (code, channel_type, locale)
);

-- ===== notification_log（per-tenant；select 租户可读自己，modify 仅 admin/服务）=====
CREATE TABLE IF NOT EXISTS notification_log (
    id              text PRIMARY KEY,
    tenant_id       text NOT NULL,
    template_code   text NOT NULL,
    channel_type    text NOT NULL,
    recipient       text NOT NULL,
    status          text NOT NULL,
    error           text NOT NULL DEFAULT '',
    provider_msg_id text NOT NULL DEFAULT '',
    created_at      timestamptz NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_notiflog_tenant_time ON notification_log(tenant_id, created_at DESC);
ALTER TABLE notification_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE notification_log FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation_select ON notification_log;
CREATE POLICY tenant_isolation_select ON notification_log
    FOR SELECT USING (tenant_id = rls_tenant_filter() OR rls_is_platform_admin());
DROP POLICY IF EXISTS tenant_isolation_modify ON notification_log;
CREATE POLICY tenant_isolation_modify ON notification_log
    FOR ALL USING (rls_is_platform_admin()) WITH CHECK (rls_is_platform_admin());

-- ===== seed 模板（幂等）=====
INSERT INTO notification_template (code, channel_type, locale, subject_tpl, body_tpl, variables_schema) VALUES
  ('task_complete', 'email', 'zh-CN',
   '【APIHub】任务完成：{{task_name}}',
   '您的任务 {{task_name}}（ID: {{task_id}}）已完成。',
   '{"type":"object","required":["task_id","task_name"],"properties":{"task_id":{"type":"string"},"task_name":{"type":"string"}}}'::jsonb),
  ('task_complete', 'dingtalk', 'zh-CN',
   '任务完成',
   '### 任务完成\n\n**{{task_name}}**（ID: `{{task_id}}`）已执行完成。',
   '{"type":"object","required":["task_id","task_name"],"properties":{"task_id":{"type":"string"},"task_name":{"type":"string"}}}'::jsonb),
  ('quota_warning', 'email', 'zh-CN',
   '【APIHub】配额预警：已用 {{used_pct}}%',
   '您本计费周期配额已使用 {{used_pct}}%（{{used}} / {{quota}}），请及时关注。',
   '{"type":"object","required":["used_pct","used","quota"],"properties":{"used_pct":{"type":"string"},"used":{"type":"string"},"quota":{"type":"string"}}}'::jsonb),
  ('invoice_ready', 'email', 'zh-CN',
   '【APIHub】账单 {{period}} 已生成',
   '您 {{period}} 的账单已生成，应付金额 {{amount}} 元。',
   '{"type":"object","required":["period","amount"],"properties":{"period":{"type":"string"},"amount":{"type":"string"}}}'::jsonb)
ON CONFLICT (code, channel_type, locale) DO NOTHING;

GRANT SELECT, INSERT, UPDATE, DELETE ON notification_channel_config, notification_log TO apihub_app;
GRANT SELECT ON notification_template TO apihub_app;

COMMIT;
