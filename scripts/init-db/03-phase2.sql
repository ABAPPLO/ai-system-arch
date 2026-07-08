-- ============================================================
-- Phase 2 schema 补丁 —— 评审工单 + 失败重试
--
-- 补全 Phase 1 schema 里缺的 3 张表：
--   - api_change_request   （api-registry 的评审工单）
--   - retry_task           （retry-svc 的重试任务主表）
--   - retry_attempt        （retry-svc 的每次重试历史）
--
-- 约定：
--   - 自增 PK 用 bigserial（高频写、不需要字符串 ID）
--   - tenant_id / api_id / app_id 全部 text，与 Phase 1 一致
--   - RLS 走老规矩：tenant_id = current_setting('app.tenant_id') OR 超管
--   - updated_at 触发器复用 01-schema.sql 里定义的 trigger_set_updated_at()
-- ============================================================

-- 守护：避免在已有库上重复建表报错
-- 所有 CREATE 都用 IF NOT EXISTS

-- ============ 1. api_change_request ============

CREATE TABLE IF NOT EXISTS api_change_request (
    id                      bigserial PRIMARY KEY,
    tenant_id               text NOT NULL,
    api_id                  text NOT NULL,
    target_version          text NOT NULL,
    change_type             text NOT NULL
                            CHECK (change_type IN ('create','update','publish','deprecate','retire')),
    target_env              text NOT NULL
                            CHECK (target_env IN ('dev','staging','prod')),
    proposed_config         jsonb NOT NULL DEFAULT '{}'::jsonb,
    current_config          jsonb,
    diff_summary            text,
    status                  text NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending','approved','rejected','applied','cancelled')),
    dingtalk_approval_id    text,
    submitted_by            text NOT NULL,
    submitted_at            timestamptz NOT NULL DEFAULT NOW(),
    reviewed_by             text,
    reviewed_at             timestamptz,
    review_comment          text,
    applied_at              timestamptz
);
CREATE INDEX IF NOT EXISTS idx_change_req_status_submitted
    ON api_change_request(status, submitted_at DESC);
CREATE INDEX IF NOT EXISTS idx_change_req_tenant_submitted
    ON api_change_request(tenant_id, submitted_at DESC);
CREATE INDEX IF NOT EXISTS idx_change_req_api_submitted
    ON api_change_request(api_id, submitted_at DESC);

-- ============ 2. retry_task ============

CREATE TABLE IF NOT EXISTS retry_task (
    id                  bigserial PRIMARY KEY,
    tenant_id           text NOT NULL,
    trace_id            text NOT NULL,
    task_instance_id    text,                       -- 关联 task 表的 id；可为空（同步调用失败也会建）
    api_id              text NOT NULL,
    app_id              text NOT NULL,
    original_request    jsonb NOT NULL DEFAULT '{}'::jsonb,
    last_error_code     text,
    last_error_msg      text,
    last_failed_at      timestamptz,
    max_attempts        integer NOT NULL,
    retry_count         integer NOT NULL DEFAULT 0,
    next_retry_at       timestamptz,
    backoff_policy      text NOT NULL
                        CHECK (backoff_policy IN ('exponential','fixed','linear')),
    backoff_base_ms     integer NOT NULL,
    status              text NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','running','succeeded','dead','ignored')),
    env                 text NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT NOW(),
    updated_at          timestamptz NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_retry_task_status_next
    ON retry_task(status, next_retry_at);
CREATE INDEX IF NOT EXISTS idx_retry_task_tenant_status
    ON retry_task(tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_retry_task_trace
    ON retry_task(trace_id);
CREATE INDEX IF NOT EXISTS idx_retry_task_api_status
    ON retry_task(api_id, status);

-- ============ 3. retry_attempt ============

CREATE TABLE IF NOT EXISTS retry_attempt (
    id                  bigserial PRIMARY KEY,
    tenant_id           text NOT NULL,
    retry_task_id       bigint NOT NULL REFERENCES retry_task(id) ON DELETE CASCADE,
    attempt_no          integer NOT NULL,
    request_body        jsonb,
    response_status     integer,
    response_body       jsonb,
    error_code          text,
    error_msg           text,
    latency_ms          integer,
    attempted_at        timestamptz NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_retry_attempt_task_no
    ON retry_attempt(retry_task_id, attempt_no);
CREATE INDEX IF NOT EXISTS idx_retry_attempt_tenant_time
    ON retry_attempt(tenant_id, attempted_at DESC);

-- ============ 4. RLS 策略 ============
-- 复用 01-schema.sql 里的 rls_tenant_filter() / rls_is_platform_admin()
-- ENABLE + FORCE：FORCE 让 owner 也受约束（业务连接用 owner 账号）。

ALTER TABLE api_change_request ENABLE ROW LEVEL SECURITY;
ALTER TABLE retry_task        ENABLE ROW LEVEL SECURITY;
ALTER TABLE retry_attempt     ENABLE ROW LEVEL SECURITY;

ALTER TABLE api_change_request FORCE ROW LEVEL SECURITY;
ALTER TABLE retry_task        FORCE ROW LEVEL SECURITY;
ALTER TABLE retry_attempt     FORCE ROW LEVEL SECURITY;

-- api_change_request
DROP POLICY IF EXISTS tenant_isolation_select ON api_change_request;
CREATE POLICY tenant_isolation_select ON api_change_request
    FOR SELECT USING (tenant_id = rls_tenant_filter() OR rls_is_platform_admin());
DROP POLICY IF EXISTS tenant_isolation_modify ON api_change_request;
CREATE POLICY tenant_isolation_modify ON api_change_request
    FOR ALL USING (tenant_id = rls_tenant_filter() OR rls_is_platform_admin())
    WITH CHECK (tenant_id = rls_tenant_filter() OR rls_is_platform_admin());

-- retry_task
DROP POLICY IF EXISTS tenant_isolation_select ON retry_task;
CREATE POLICY tenant_isolation_select ON retry_task
    FOR SELECT USING (tenant_id = rls_tenant_filter() OR rls_is_platform_admin());
DROP POLICY IF EXISTS tenant_isolation_modify ON retry_task;
CREATE POLICY tenant_isolation_modify ON retry_task
    FOR ALL USING (tenant_id = rls_tenant_filter() OR rls_is_platform_admin())
    WITH CHECK (tenant_id = rls_tenant_filter() OR rls_is_platform_admin());

-- retry_attempt
DROP POLICY IF EXISTS tenant_isolation_select ON retry_attempt;
CREATE POLICY tenant_isolation_select ON retry_attempt
    FOR SELECT USING (tenant_id = rls_tenant_filter() OR rls_is_platform_admin());
DROP POLICY IF EXISTS tenant_isolation_modify ON retry_attempt;
CREATE POLICY tenant_isolation_modify ON retry_attempt
    FOR ALL USING (tenant_id = rls_tenant_filter() OR rls_is_platform_admin())
    WITH CHECK (tenant_id = rls_tenant_filter() OR rls_is_platform_admin());

-- ============ 5. updated_at 触发器 ============
-- 复用 01-schema.sql 已定义的 trigger_set_updated_at() 函数
-- 注意：trigger_set_updated_at() 引用 NEW.updated_at，因此只能挂到有 updated_at 列的表。
-- api_change_request / retry_task 都没有 updated_at 列，Phase 2 起就不再挂（之前误挂导致 UPDATE 报 "record new has no field updated_at"）。

DO $$
DECLARE t text;
BEGIN
    FOR t IN SELECT unnest(ARRAY[
        'api_change_request', 'retry_task'
    ])
    LOOP
        EXECUTE format($f$ DROP TRIGGER IF EXISTS set_updated_at ON %I $f$, t);
    END LOOP;
END $$;

-- ============ 6. 自检 ============
-- SET app.tenant_id = 'tenant_a';
-- SELECT * FROM retry_task;          -- 应只看到 tenant_a 的
-- SELECT * FROM api_change_request;  -- 同上
