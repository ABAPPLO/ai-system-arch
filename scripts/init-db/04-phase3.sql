-- ============================================================
-- Phase 3 schema 补丁 —— workflow-svc 工作流实例索引表
--
-- 补 Phase 1/2 都漏建的 workflow_instance 表（workflow-svc 的 PG 索引表）。
-- 此前该表只在 workflow/README.md 与 repository.py docstring 里描述过，
-- 但从未落到 init-db，导致 e2e（APISIX -> dispatcher /v1/jobs -> workflow-svc）
-- 首次跑到 INSERT 时报 "relation workflow_instance does not exist"。
--
-- 约定（与 03-phase2.sql 一致）：
--   - 自增 PK 用 bigserial（workflow-svc 内部 id；对外路由 /v1/workflows/{id}）
--   - tenant_id / api_id / app_id 全部 text，与 Phase 1 一致
--     （tenant_id='tenant_a'、api_id='api_demo_a' 等都是 text；不要 BIGINT）
--   - RLS 走老规矩：tenant_id = current_setting('app.tenant_id') OR 超管
--   - updated_at 触发器复用 01-schema.sql 里定义的 trigger_set_updated_at()
-- ============================================================

-- 守护：避免在已有库上重复建表报错
CREATE TABLE IF NOT EXISTS workflow_instance (
    id              bigserial PRIMARY KEY,
    tenant_id       text NOT NULL,
    workflow_uuid   varchar(64) NOT NULL UNIQUE,
    argo_name       varchar(128) NOT NULL,
    namespace       varchar(64) NOT NULL DEFAULT 'apihub-workflow',
    api_id          text,
    app_id          text,
    trace_id        varchar(64),
    spec            jsonb NOT NULL,
    status          varchar(20) NOT NULL DEFAULT 'submitted',
    message         text,
    submitted_at    timestamptz NOT NULL DEFAULT NOW(),
    finished_at     timestamptz,
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    updated_at      timestamptz NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_wf_tenant_status
    ON workflow_instance(tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_wf_tenant_submitted
    ON workflow_instance(tenant_id, submitted_at DESC);

-- ============ RLS ============
-- 复用 01-schema.sql 里的 rls_tenant_filter() / rls_is_platform_admin()
-- ENABLE + FORCE：FORCE 让 owner 也受约束（业务连接用 owner 账号）。

ALTER TABLE workflow_instance ENABLE ROW LEVEL SECURITY;
ALTER TABLE workflow_instance FORCE  ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_select ON workflow_instance;
CREATE POLICY tenant_isolation_select ON workflow_instance
    FOR SELECT USING (tenant_id = rls_tenant_filter() OR rls_is_platform_admin());
DROP POLICY IF EXISTS tenant_isolation_modify ON workflow_instance;
CREATE POLICY tenant_isolation_modify ON workflow_instance
    FOR ALL USING (tenant_id = rls_tenant_filter() OR rls_is_platform_admin())
    WITH CHECK (tenant_id = rls_tenant_filter() OR rls_is_platform_admin());

-- ============ updated_at 触发器 ============
-- workflow_instance 有 updated_at 列，挂 trigger_set_updated_at()
-- （01-schema.sql 里定义；该函数引用 NEW.updated_at，故只挂有该列的表）。
DROP TRIGGER IF EXISTS set_updated_at ON workflow_instance;
CREATE TRIGGER set_updated_at BEFORE UPDATE ON workflow_instance
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();

-- ============ 授权 ============
-- 99-grants.sql 用 GRANT ... ON ALL TABLES，在它之后跑会自动覆盖；
-- 这里补一道是为了单独 apply 本文件（不走全量 init）时也能让 apihub_app 访问。
GRANT SELECT, INSERT, UPDATE, DELETE ON workflow_instance TO apihub_app;
GRANT USAGE, SELECT ON SEQUENCE workflow_instance_id_seq TO apihub_app;
