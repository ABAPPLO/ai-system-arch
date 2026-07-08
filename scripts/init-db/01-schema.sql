-- ============================================================
-- APIHub 核心元数据 schema（与 docs/04-data-model.md 对齐）
-- 含：表 + 索引 + RLS 策略 + 触发器
-- ============================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;        -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS pg_trgm;         -- 文本模糊索引

-- 启用 RLS（每个租户感知的表都要 ALTER ... ENABLE ROW LEVEL SECURITY）
SET app.tenant_id = '';  -- 初始化阶段 placeholder

-- ============================================================
-- 1. 租户 / 用户 / 成员
-- ============================================================

CREATE TABLE IF NOT EXISTS tenant (
    id              text PRIMARY KEY,
    parent_id       text REFERENCES tenant(id),
    name            text NOT NULL,
    slug            text UNIQUE NOT NULL,
    type            text NOT NULL CHECK (type IN ('internal', 'external', 'system')),
    status          text NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'suspended', 'closed')),
    tier            text NOT NULL DEFAULT 'standard',  -- free/standard/premium
    metadata        jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    updated_at      timestamptz NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_tenant_parent ON tenant(parent_id) WHERE parent_id IS NOT NULL;
-- 租户表本身不做 RLS（无 tenant_id 列），由应用层超管管理

CREATE TABLE IF NOT EXISTS user_account (
    id              text PRIMARY KEY,
    email           text UNIQUE NOT NULL,
    phone           text,
    password_hash   text,                          -- bcrypt / argon2
    name            text NOT NULL,
    avatar_url      text,
    verification_level text NOT NULL DEFAULT 'email'
                    CHECK (verification_level IN ('email', 'email_phone', 'enterprise')),
    status          text NOT NULL DEFAULT 'active',
    last_login_at   timestamptz,
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    updated_at      timestamptz NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS tenant_member (
    id              text PRIMARY KEY,
    tenant_id       text NOT NULL REFERENCES tenant(id),
    user_id         text NOT NULL REFERENCES user_account(id),
    role            text NOT NULL DEFAULT 'developer'
                    CHECK (role IN ('owner', 'admin', 'developer', 'viewer')),
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, user_id)
);
CREATE INDEX idx_tenant_member_user ON tenant_member(user_id);

-- ============================================================
-- 2. 应用 + API Key（调用方凭证）
-- ============================================================

CREATE TABLE IF NOT EXISTS app (
    id              text PRIMARY KEY,
    tenant_id       text NOT NULL REFERENCES tenant(id),
    name            text NOT NULL,
    type            text NOT NULL DEFAULT 'internal'
                    CHECK (type IN ('internal', 'external', 'web', 'mobile', 'server')),
    status          text NOT NULL DEFAULT 'active',
    quota_tier      text NOT NULL DEFAULT 'standard',
    metadata        jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    updated_at      timestamptz NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_app_tenant ON app(tenant_id);

CREATE TABLE IF NOT EXISTS api_key (
    id              text PRIMARY KEY,
    tenant_id       text NOT NULL REFERENCES tenant(id),
    app_id          text NOT NULL REFERENCES app(id),
    key_prefix      text NOT NULL,                -- 前 8 位明文（展示用）
    key_hash        text NOT NULL UNIQUE,         -- sha256
    name            text NOT NULL,
    scopes          text[] NOT NULL DEFAULT '{}',
    status          text NOT NULL DEFAULT 'active',
    last_used_at    timestamptz,
    expires_at      timestamptz,
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    revoked_at      timestamptz,
    revoked_reason  text
);
CREATE INDEX idx_api_key_tenant ON api_key(tenant_id);
CREATE INDEX idx_api_key_hash ON api_key(key_hash);

-- ============================================================
-- 3. 接口元数据
-- ============================================================

CREATE TABLE IF NOT EXISTS api (
    id              text PRIMARY KEY,
    tenant_id       text NOT NULL REFERENCES tenant(id),
    name            text NOT NULL,
    description     text,
    category        text NOT NULL,
    base_path       text NOT NULL,
    tags            text[] NOT NULL DEFAULT '{}',
    owner_team      text,
    status          text NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft', 'reviewing', 'published', 'deprecated', 'retired')),
    visibility      text NOT NULL DEFAULT 'private'
                    CHECK (visibility IN ('private', 'tenant', 'public')),
    metadata        jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    updated_at      timestamptz NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, base_path)
);
CREATE INDEX idx_api_tenant ON api(tenant_id);
CREATE INDEX idx_api_category ON api(category);
CREATE INDEX idx_api_name_trgm ON api USING gin (name gin_trgm_ops);

CREATE TABLE IF NOT EXISTS api_version (
    id              text PRIMARY KEY,
    tenant_id       text NOT NULL REFERENCES tenant(id),
    api_id          text NOT NULL REFERENCES api(id),
    version         text NOT NULL,                -- v1, v2
    backend_type    text NOT NULL
                    CHECK (backend_type IN ('http', 'async_task', 'workflow', 'ai_model')),
    backend_url     text NOT NULL,
    method          text NOT NULL,
    path            text NOT NULL,
    request_schema  jsonb,
    response_schema jsonb,
    masking         jsonb,                        -- 字段脱敏规则
    rate_limit      jsonb,                        -- {count, window_seconds}
    retry_policy    jsonb,                        -- {max_attempts, backoff_seconds, multiplier}
    cache_policy    jsonb,                        -- {enabled, ttl_seconds, vary_by[]}
    auth_policy     jsonb,                        -- {methods[], scopes[]}
    -- AI 网关字段（backend_type='ai_model'）
    ai_model        text,
    ai_streaming    boolean NOT NULL DEFAULT false,
    ai_params       jsonb,
    -- SLA
    sla_p99_ms      integer,
    sla_availability numeric(5,4),
    -- 生命周期
    status          text NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft', 'reviewing', 'published', 'deprecated', 'retired')),
    published_at    timestamptz,
    retired_at      timestamptz,
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    updated_at      timestamptz NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, api_id, version)
);
CREATE INDEX idx_api_version_tenant ON api_version(tenant_id);
CREATE INDEX idx_api_version_api ON api_version(api_id);
CREATE INDEX idx_api_version_status ON api_version(status);

-- ============================================================
-- 4. 异步任务实例（dispatcher 写，executor 改，调用方查）
-- ============================================================
-- docs/04-data-model.md §2.7 + docs/05-core-flows.md §3
-- 关键：status 状态机 pending → running → succeeded/failed/timeout/cancelled

CREATE TABLE IF NOT EXISTS task (
    id              text PRIMARY KEY,            -- "task_xxxx"（dispatcher 生成）
    tenant_id       text NOT NULL,
    api_id          text NOT NULL,
    api_version_id  text NOT NULL,
    app_id          text NOT NULL,
    status          text NOT NULL
                    CHECK (status IN ('pending', 'running', 'succeeded', 'failed', 'timeout', 'cancelled')),
    payload         text,                        -- 原始请求 body（可能是非 JSON）
    response_body   text,                        -- 后端响应 body
    response_status integer,                     -- 后端 HTTP status
    error_code      text,
    error_msg       text,
    request_id      text,
    trace_id        text,
    callback_url    text,                        -- 完成后回调（可选）
    retry_count     integer NOT NULL DEFAULT 0,
    started_at      timestamptz,
    finished_at     timestamptz,
    timeout_at      timestamptz,
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    updated_at      timestamptz NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_task_tenant_status ON task(tenant_id, status);
CREATE INDEX idx_task_tenant_app_created ON task(tenant_id, app_id, created_at DESC);
CREATE INDEX idx_task_status_created ON task(status, created_at) WHERE status IN ('pending', 'running');
CREATE INDEX idx_task_request_id ON task(request_id);

-- ============================================================
-- 5. 审计日志（合规底线：在线 6 月 + OSS 永久）
-- ============================================================

CREATE TABLE IF NOT EXISTS audit_log (
    id              bigserial PRIMARY KEY,
    tenant_id       text NOT NULL,
    actor_type      text NOT NULL,                -- user / app / system
    actor_id        text,
    actor_name      text,
    actor_ip        inet,
    auth_method     text,                         -- 等保三级要求
    action          text NOT NULL,
    resource_type   text NOT NULL,
    resource_id     text,
    resource_name   text,
    env             text,
    detail          jsonb,                        -- before/after diff
    user_agent      text,
    request_id      text,
    trace_id        text,
    created_at      timestamptz NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_audit_tenant_time ON audit_log(tenant_id, created_at DESC);
CREATE INDEX idx_audit_actor ON audit_log(actor_id, created_at DESC);
CREATE INDEX idx_audit_resource ON audit_log(resource_type, resource_id, created_at DESC);
CREATE INDEX idx_audit_action ON audit_log(action);

-- ============================================================
-- 6. RLS 策略 —— 所有带 tenant_id 的表强制隔离
-- ============================================================
-- 详见 docs/04-data-model.md §5 + docs/11-multi-tenant.md §4
--
-- 关键：
--   1. 业务代码不写 WHERE tenant_id=?，由 RLS 自动过滤
--   2. PG session 必须先 SET LOCAL app.tenant_id = ?
--   3. is_platform_admin=true 时绕过 RLS（仅超管跨租户）

-- 启用 RLS
-- ENABLE 让策略生效；FORCE 让表 owner 也受策略约束（否则业务连接的 apihub
-- 用户因为是 owner 会绕过 RLS，等价于 RLS 没装）。两者都要。
ALTER TABLE tenant_member   ENABLE ROW LEVEL SECURITY;
ALTER TABLE app             ENABLE ROW LEVEL SECURITY;
ALTER TABLE api_key         ENABLE ROW LEVEL SECURITY;
ALTER TABLE api             ENABLE ROW LEVEL SECURITY;
ALTER TABLE api_version     ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log       ENABLE ROW LEVEL SECURITY;
ALTER TABLE task            ENABLE ROW LEVEL SECURITY;

ALTER TABLE tenant_member   FORCE ROW LEVEL SECURITY;
ALTER TABLE app             FORCE ROW LEVEL SECURITY;
ALTER TABLE api_key         FORCE ROW LEVEL SECURITY;
ALTER TABLE api             FORCE ROW LEVEL SECURITY;
ALTER TABLE api_version     FORCE ROW LEVEL SECURITY;
ALTER TABLE audit_log       FORCE ROW LEVEL SECURITY;
ALTER TABLE task            FORCE ROW LEVEL SECURITY;

-- 通用策略宏：用 SQL 减少重复
-- 当前租户可见，超管可见全部
CREATE OR REPLACE FUNCTION rls_tenant_filter() RETURNS text AS $$
    SELECT current_setting('app.tenant_id', true)
$$ LANGUAGE sql STABLE;

CREATE OR REPLACE FUNCTION rls_is_platform_admin() RETURNS boolean AS $$
    SELECT COALESCE(current_setting('app.is_platform_admin', true)::boolean, false)
$$ LANGUAGE sql STABLE;

-- 通用策略：tenant 等于当前会话租户，或超管
DROP POLICY IF EXISTS tenant_isolation_select ON tenant_member;
CREATE POLICY tenant_isolation_select ON tenant_member
    FOR SELECT USING (
        tenant_id = rls_tenant_filter() OR rls_is_platform_admin()
    );
DROP POLICY IF EXISTS tenant_isolation_modify ON tenant_member;
CREATE POLICY tenant_isolation_modify ON tenant_member
    FOR ALL USING (
        tenant_id = rls_tenant_filter() OR rls_is_platform_admin()
    )
    WITH CHECK (
        tenant_id = rls_tenant_filter() OR rls_is_platform_admin()
    );

-- 同样模式应用到其他表（重复造避免 SQL 注入）
DROP POLICY IF EXISTS tenant_isolation_select ON app;
CREATE POLICY tenant_isolation_select ON app
    FOR SELECT USING (tenant_id = rls_tenant_filter() OR rls_is_platform_admin());
DROP POLICY IF EXISTS tenant_isolation_modify ON app;
CREATE POLICY tenant_isolation_modify ON app
    FOR ALL USING (tenant_id = rls_tenant_filter() OR rls_is_platform_admin())
    WITH CHECK (tenant_id = rls_tenant_filter() OR rls_is_platform_admin());

DROP POLICY IF EXISTS tenant_isolation_select ON api_key;
CREATE POLICY tenant_isolation_select ON api_key
    FOR SELECT USING (tenant_id = rls_tenant_filter() OR rls_is_platform_admin());
DROP POLICY IF EXISTS tenant_isolation_modify ON api_key;
CREATE POLICY tenant_isolation_modify ON api_key
    FOR ALL USING (tenant_id = rls_tenant_filter() OR rls_is_platform_admin())
    WITH CHECK (tenant_id = rls_tenant_filter() OR rls_is_platform_admin());

DROP POLICY IF EXISTS tenant_isolation_select ON api;
CREATE POLICY tenant_isolation_select ON api
    FOR SELECT USING (tenant_id = rls_tenant_filter() OR rls_is_platform_admin());
DROP POLICY IF EXISTS tenant_isolation_modify ON api;
CREATE POLICY tenant_isolation_modify ON api
    FOR ALL USING (tenant_id = rls_tenant_filter() OR rls_is_platform_admin())
    WITH CHECK (tenant_id = rls_tenant_filter() OR rls_is_platform_admin());

DROP POLICY IF EXISTS tenant_isolation_select ON api_version;
CREATE POLICY tenant_isolation_select ON api_version
    FOR SELECT USING (tenant_id = rls_tenant_filter() OR rls_is_platform_admin());
DROP POLICY IF EXISTS tenant_isolation_modify ON api_version;
CREATE POLICY tenant_isolation_modify ON api_version
    FOR ALL USING (tenant_id = rls_tenant_filter() OR rls_is_platform_admin())
    WITH CHECK (tenant_id = rls_tenant_filter() OR rls_is_platform_admin());

DROP POLICY IF EXISTS tenant_isolation_select ON audit_log;
CREATE POLICY tenant_isolation_select ON audit_log
    FOR SELECT USING (tenant_id = rls_tenant_filter() OR rls_is_platform_admin());
DROP POLICY IF EXISTS tenant_isolation_modify ON audit_log;
CREATE POLICY tenant_isolation_modify ON audit_log
    FOR ALL USING (tenant_id = rls_tenant_filter() OR rls_is_platform_admin())
    WITH CHECK (tenant_id = rls_tenant_filter() OR rls_is_platform_admin());

DROP POLICY IF EXISTS tenant_isolation_select ON task;
CREATE POLICY tenant_isolation_select ON task
    FOR SELECT USING (tenant_id = rls_tenant_filter() OR rls_is_platform_admin());
DROP POLICY IF EXISTS tenant_isolation_modify ON task;
CREATE POLICY tenant_isolation_modify ON task
    FOR ALL USING (tenant_id = rls_tenant_filter() OR rls_is_platform_admin())
    WITH CHECK (tenant_id = rls_tenant_filter() OR rls_is_platform_admin());

-- ============================================================
-- 7. updated_at 自动更新触发器
-- ============================================================

CREATE OR REPLACE FUNCTION trigger_set_updated_at()
RETURNS trigger AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE t text;
BEGIN
    FOR t IN SELECT unnest(ARRAY[
        'tenant', 'user_account', 'app', 'api', 'api_version', 'task'
    ])
    LOOP
        EXECUTE format($f$
            CREATE TRIGGER set_updated_at BEFORE UPDATE ON %I
            FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at()
        $f$, t);
    END LOOP;
END $$;

-- ============================================================
-- 8. 测试连通性（开发期自检）
-- ============================================================
-- 验证 RLS：开两个 session 模拟
--   SET app.tenant_id = 'tenant_a'; SELECT count(*) FROM api;  -- 只看到 A 的
--   SET app.tenant_id = 'tenant_b'; SELECT count(*) FROM api;  -- 只看到 B 的
