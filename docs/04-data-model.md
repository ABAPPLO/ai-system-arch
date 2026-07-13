# 04 · 数据模型

> 本文档反映 [ADR-009 多租户](00-decisions.md#adr-009-多租户策略) 和 [ADR-004 AI 网关预留](00-decisions.md#adr-004-ai-网关扩展) 决策。
>
> 所有元数据表加 `tenant_id` 字段，AI 网关字段已预留，billing 表标注 Phase 3 启用。

## 1. 存储分布

| 数据类型 | 存储 | 读写特征 |
|---------|------|---------|
| 租户 / 用户 / 成员关系 | PostgreSQL | 强一致、小写大读 |
| 接口元数据 | PostgreSQL | 强一致、小写大读 |
| 调用方 / 应用 / Key | PostgreSQL | 强一致 |
| 授权关系 | PostgreSQL + Redis 缓存 | 强一致 + 高读 |
| 配额规则 | PostgreSQL | 强一致 |
| 实时配额计数 | Redis（按租户分 key） | 极高读写 |
| 调用日志 | ClickHouse | 极高写、按租户聚合读 |
| 错误堆栈 / 大 body | MinIO | 写多读少 |
| 链路 Span | Jaeger (Cassandra/ES) | 高写、按 trace 查 |
| 重试任务 | PostgreSQL + Redis ZSet | 高写 |
| 审计日志 | PostgreSQL + OSS 归档（保留 6 个月+） | 中等写，等保三级要求 |
| 接口元数据缓存 | Redis Cluster | 极高读 |
| 网关路由配置 | etcd（APISIX） | 高读 |
| 计费数据（Phase 3） | PostgreSQL + ClickHouse | T+1 聚合 |

## 2. PostgreSQL Schema

> **强制约定**：所有元数据表首位字段为 `tenant_id`，索引首位也是 `tenant_id`。
> 应用层 + RLS 双重隔离，详见 [11-multi-tenant.md](11-multi-tenant.md)。

### 2.1 租户与成员

> 多 Region 双活架构下（参见 ADR-013），每个租户拥有一个 `home_region` 决定写流量路由到哪个区域。读流量可从任一区域读（PG 逻辑复制 + CH 双写）。

```sql
CREATE TABLE tenant (
    id              BIGSERIAL PRIMARY KEY,
    uuid            UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    code            VARCHAR(64) NOT NULL UNIQUE,                -- 'internal-user-svc'
    name            VARCHAR(128) NOT NULL,
    type            VARCHAR(20) NOT NULL,                       -- system/internal/external
    parent_id       BIGINT REFERENCES tenant(id),               -- 父租户（支持层级）
    status          VARCHAR(20) NOT NULL DEFAULT 'active',      -- active/suspended/closed
    home_region     VARCHAR(20) NOT NULL DEFAULT 'sh',          -- 归属区域 sh=cn-shanghai, bj=cn-beijing；ADR-013
    quota_config    JSONB DEFAULT '{}',                         -- 租户级配额默认
    rate_limit      JSONB DEFAULT '{}',                         -- 租户级限流
    contact_email   VARCHAR(128),
    contact_phone   VARCHAR(32),
    metadata        JSONB DEFAULT '{}',                         -- 自定义元数据
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_tenant_parent ON tenant(parent_id);
CREATE INDEX idx_tenant_type ON tenant(type);

-- 默认租户（系统初始化时插入）
INSERT INTO tenant (id, code, name, type) VALUES
    (1, 'system', 'Platform System', 'system'),
    (2, 'internal-default', 'Default Internal', 'internal'),
    (3, 'external-public', 'External Public', 'external');

-- 用户（跨租户共享一个账号）
CREATE TABLE user_account (
    id              BIGSERIAL PRIMARY KEY,
    uuid            UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    email           VARCHAR(128) NOT NULL UNIQUE,
    phone           VARCHAR(32),
    password_hash   VARCHAR(128),                               -- 内部账号密码（外部用 OAuth 时可空）
    name            VARCHAR(128),
    avatar          VARCHAR(512),
    email_verified  BOOLEAN NOT NULL DEFAULT FALSE,
    phone_verified  BOOLEAN NOT NULL DEFAULT FALSE,
    verification_level VARCHAR(20) NOT NULL DEFAULT 'basic',    -- basic/enterprise
    status          VARCHAR(20) NOT NULL DEFAULT 'active',
    last_login_at   TIMESTAMPTZ,
    last_login_ip   INET,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 用户与租户的多对多关系
CREATE TABLE tenant_member (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       BIGINT NOT NULL REFERENCES tenant(id),
    user_id         BIGINT NOT NULL REFERENCES user_account(id),
    role            VARCHAR(32) NOT NULL,                       -- owner/admin/developer/viewer
    invited_by      BIGINT REFERENCES user_account(id),
    invited_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    joined_at       TIMESTAMPTZ,
    status          VARCHAR(20) NOT NULL DEFAULT 'pending',     -- pending/active/removed
    
    UNIQUE (tenant_id, user_id)
);

CREATE INDEX idx_member_tenant ON tenant_member(tenant_id, status);
CREATE INDEX idx_member_user ON tenant_member(user_id);
```

### 2.2 接口定义

```sql
CREATE TABLE api_definition (
    tenant_id       BIGINT NOT NULL REFERENCES tenant(id),     -- ⭐ 多租户
    id              BIGSERIAL PRIMARY KEY,
    uuid            UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    name            VARCHAR(128) NOT NULL,
    display_name    VARCHAR(256) NOT NULL,
    description     TEXT,
    path            VARCHAR(512) NOT NULL,
    method          VARCHAR(10) NOT NULL,
    mode            VARCHAR(20) NOT NULL,                       -- sync/async/workflow
    service_name    VARCHAR(128),
    tags            JSONB DEFAULT '[]',
    owner           VARCHAR(64) NOT NULL,
    team            VARCHAR(64),
    biz_line        VARCHAR(64),
    status          VARCHAR(20) NOT NULL DEFAULT 'draft',
    visibility      VARCHAR(20) NOT NULL DEFAULT 'private',     -- private/internal/public/cross_tenant
    min_verification_level VARCHAR(20) DEFAULT 'basic',         -- 接入方需达成的认证级别
    source          VARCHAR(20) NOT NULL DEFAULT 'ui',          -- ui/yaml/migration
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at      TIMESTAMPTZ,
    
    UNIQUE (tenant_id, path, method) WHERE deleted_at IS NULL
);

CREATE INDEX idx_api_def_tenant_status ON api_definition(tenant_id, status) WHERE deleted_at IS NULL;
CREATE INDEX idx_api_def_tenant_owner ON api_definition(tenant_id, owner);
CREATE INDEX idx_api_def_tenant_tags ON api_definition USING GIN (tenant_id, tags);
```

### 2.3 接口版本

```sql
CREATE TABLE api_version (
    tenant_id       BIGINT NOT NULL,
    id              BIGSERIAL PRIMARY KEY,
    api_id          BIGINT NOT NULL REFERENCES api_definition(id),
    version         VARCHAR(32) NOT NULL,
    changelog       TEXT,
    -- 鉴权
    auth_type       VARCHAR(32) NOT NULL,                       -- none/jwt/apikey/sign/oauth2
    auth_config     JSONB NOT NULL DEFAULT '{}',
    -- 限流
    rate_limit      JSONB DEFAULT '{}',
    -- 超时与重试
    timeout_ms      INT NOT NULL DEFAULT 3000,
    retry_policy    JSONB DEFAULT '{}',
    idempotent      BOOLEAN NOT NULL DEFAULT FALSE,             -- 是否幂等（决定能否自动重试）
    -- 后端实现（含 AI 模型）
    backend_type    VARCHAR(20) NOT NULL,                       -- http/grpc/script/mq/ai_model
    backend_config  JSONB NOT NULL,                             -- 含 AI 模型路由、prompt 模板等
    -- AI 网关扩展（backend_type=ai_model 时使用）
    ai_model        VARCHAR(64),                                -- openai-gpt4/anthropic-claude/...
    ai_streaming    BOOLEAN DEFAULT FALSE,                      -- 是否流式输出
    ai_token_limit  INT,                                        -- 单次最大 token
    -- Schema
    request_schema  JSONB NOT NULL,
    response_schema JSONB NOT NULL,
    error_schemas   JSONB DEFAULT '{}',
    examples        JSONB DEFAULT '[]',
    -- 环境
    env_status      JSONB DEFAULT '{"dev":"draft","staging":"draft","prod":"draft"}',
    -- 元数据
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    published_at    TIMESTAMPTZ,
    deprecated_at   TIMESTAMPTZ,
    retired_at      TIMESTAMPTZ,
    created_by      VARCHAR(64) NOT NULL,
    
    UNIQUE (tenant_id, api_id, version)
);

CREATE INDEX idx_api_version_tenant_api ON api_version(tenant_id, api_id);
CREATE INDEX idx_api_version_tenant_backend ON api_version(tenant_id, backend_type);

-- AI 模型路由配置（backend_type=ai_model 时，backend_config 示例）
-- {
--   "model": "gpt-4-turbo",
--   "provider": "openai",
--   "api_key_ref": "vault://ai/openai-key",
--   "prompt_template": "...",
--   "temperature": 0.7,
--   "max_tokens": 2000,
--   "fallback_model": "gpt-3.5-turbo"
-- }
```

### 2.4 调用方应用

```sql
CREATE TABLE app (
    tenant_id       BIGINT NOT NULL REFERENCES tenant(id),
    id              BIGSERIAL PRIMARY KEY,
    uuid            UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    name            VARCHAR(128) NOT NULL,
    app_type        VARCHAR(20) NOT NULL,                       -- internal/external
    owner           VARCHAR(64) NOT NULL,
    team            VARCHAR(64),
    contact_email   VARCHAR(128),
    callback_url    VARCHAR(512),
    webhook_secret  VARCHAR(128),
    status          VARCHAR(20) NOT NULL DEFAULT 'active',
    daily_quota     INT,
    rate_limit      JSONB DEFAULT '{}',
    tags            JSONB DEFAULT '[]',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_app_tenant_owner ON app(tenant_id, owner);
CREATE INDEX idx_app_tenant_status ON app(tenant_id, status);
```

### 2.5 API Key

```sql
CREATE TABLE app_api_key (
    tenant_id       BIGINT NOT NULL,
    id              BIGSERIAL PRIMARY KEY,
    app_id          BIGINT NOT NULL REFERENCES app(id),
    key_id          VARCHAR(64) NOT NULL UNIQUE,                -- ak_xxxxxxxx
    key_prefix      VARCHAR(20) NOT NULL,
    key_hash        VARCHAR(128) NOT NULL,                      -- SHA256(secret)
    name            VARCHAR(128) NOT NULL,
    scopes          JSONB DEFAULT '[]',
    expires_at      TIMESTAMPTZ,                                -- 外部 Key 默认 +365 天
    last_used_at    TIMESTAMPTZ,
    last_used_ip    INET,
    last_rotated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),         -- ADR-006 轮换提示
    rotation_notified_at TIMESTAMPTZ,                           -- 上次提醒时间
    status          VARCHAR(20) NOT NULL DEFAULT 'active',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at      TIMESTAMPTZ,
    revoked_reason  VARCHAR(256)
);

CREATE INDEX idx_apikey_tenant_app ON app_api_key(tenant_id, app_id);
CREATE INDEX idx_apikey_hash ON app_api_key(key_hash) WHERE status = 'active';
```

### 2.6 授权关系

```sql
CREATE TABLE app_api_grant (
    tenant_id       BIGINT NOT NULL,                            -- 调用方租户
    id              BIGSERIAL PRIMARY KEY,
    app_id          BIGINT NOT NULL REFERENCES app(id),
    api_id          BIGINT NOT NULL REFERENCES api_definition(id),
    api_tenant_id   BIGINT NOT NULL,                            -- 接口提供方租户（跨租户场景）
    api_version     VARCHAR(32) NOT NULL,
    granted_by      VARCHAR(64) NOT NULL,
    granted_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ,
    custom_quota    JSONB,
    status          VARCHAR(20) NOT NULL DEFAULT 'active',
    
    UNIQUE (tenant_id, app_id, api_id, api_version)
);

CREATE INDEX idx_grant_tenant_app ON app_api_grant(tenant_id, app_id);
CREATE INDEX idx_grant_api_tenant ON app_api_grant(api_tenant_id, api_id);
```

### 2.7 异步任务实例

```sql
CREATE TABLE task_instance (
    tenant_id       BIGINT NOT NULL,
    id              BIGUUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trace_id        VARCHAR(64) NOT NULL,
    api_id          BIGINT NOT NULL,
    api_version     VARCHAR(32) NOT NULL,
    app_id          BIGINT NOT NULL,
    mode            VARCHAR(20) NOT NULL,                       -- async/workflow
    status          VARCHAR(20) NOT NULL,                       -- pending/running/succeeded/failed/cancelled/timeout
    request_body    JSONB,
    response_body   JSONB,
    error_code      VARCHAR(64),
    error_msg       TEXT,
    error_stack     TEXT,
    callback_url    VARCHAR(512),
    callback_status VARCHAR(20),
    retry_count     INT NOT NULL DEFAULT 0,
    -- AI 网关扩展
    token_prompt    INT DEFAULT 0,
    token_completion INT DEFAULT 0,
    token_total     INT DEFAULT 0,
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    timeout_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    env             VARCHAR(20) NOT NULL
) PARTITION BY RANGE (created_at);

CREATE TABLE task_instance_2026_07 PARTITION OF task_instance
  FOR VALUES FROM ('2026-07-01') TO ('2026-08-01');

CREATE INDEX idx_task_tenant_trace ON task_instance(tenant_id, trace_id);
CREATE INDEX idx_task_tenant_api_status ON task_instance(tenant_id, api_id, status);
CREATE INDEX idx_task_tenant_app ON task_instance(tenant_id, app_id, created_at DESC);
```

### 2.8 重试任务

```sql
CREATE TABLE retry_task (
    tenant_id       BIGINT NOT NULL,
    id              BIGSERIAL PRIMARY KEY,
    trace_id        VARCHAR(64) NOT NULL,
    task_instance_id BIGUUID,
    api_id          BIGINT NOT NULL,
    app_id          BIGINT NOT NULL,
    original_request JSONB NOT NULL,
    last_error_code VARCHAR(64),
    last_error_msg  TEXT,
    last_error_stack TEXT,
    last_failed_at  TIMESTAMPTZ,
    max_attempts    INT NOT NULL DEFAULT 3,
    retry_count     INT NOT NULL DEFAULT 0,
    next_retry_at   TIMESTAMPTZ,
    backoff_policy  VARCHAR(32) NOT NULL DEFAULT 'exponential',
    backoff_base_ms INT NOT NULL DEFAULT 1000,
    status          VARCHAR(20) NOT NULL DEFAULT 'pending',
    env             VARCHAR(20) NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_retry_tenant_status_next ON retry_task(tenant_id, status, next_retry_at);

CREATE TABLE retry_attempt (
    tenant_id       BIGINT NOT NULL,
    id              BIGSERIAL PRIMARY KEY,
    retry_task_id   BIGINT NOT NULL REFERENCES retry_task(id),
    attempt_no      INT NOT NULL,
    request_body    JSONB,
    response_status INT,
    response_body   JSONB,
    error_code      VARCHAR(64),
    error_msg       TEXT,
    latency_ms      INT,
    attempted_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_attempt_tenant_retry ON retry_attempt(tenant_id, retry_task_id);
```

### 2.9 审计日志（等保 2.0 三级要求保留 6 个月+）

```sql
CREATE TABLE audit_log (
    tenant_id       BIGINT NOT NULL,
    id              BIGSERIAL PRIMARY KEY,
    actor_type      VARCHAR(20) NOT NULL,                       -- user/app/system
    actor_id        VARCHAR(64) NOT NULL,
    actor_name      VARCHAR(128),
    actor_ip        INET,
    action          VARCHAR(64) NOT NULL,
    resource_type   VARCHAR(32) NOT NULL,
    resource_id     VARCHAR(64),
    resource_name   VARCHAR(256),
    env             VARCHAR(20),
    detail          JSONB,                                      -- 变更前后 diff
    user_agent      VARCHAR(256),
    request_id      VARCHAR(64),
    auth_method     VARCHAR(32),                                -- 鉴权方式（合规要求）
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
) PARTITION BY RANGE (created_at);

CREATE INDEX idx_audit_tenant_actor ON audit_log(tenant_id, actor_id, created_at DESC);
CREATE INDEX idx_audit_tenant_resource ON audit_log(tenant_id, resource_type, resource_id);
CREATE INDEX idx_audit_tenant_action ON audit_log(tenant_id, action, created_at DESC);
```

### 2.10 接口变更评审工单

```sql
CREATE TABLE api_change_request (
    tenant_id       BIGINT NOT NULL,
    id              BIGSERIAL PRIMARY KEY,
    api_id          BIGINT NOT NULL,
    target_version  VARCHAR(32) NOT NULL,
    change_type     VARCHAR(32) NOT NULL,                       -- create/update/publish/deprecate
    target_env      VARCHAR(20) NOT NULL,                       -- dev/staging/prod
    proposed_config JSONB NOT NULL,
    current_config  JSONB,
    diff_summary    TEXT,
    status          VARCHAR(20) NOT NULL DEFAULT 'pending',     -- pending/approved/rejected/applied/cancelled
    -- 钉钉审批集成（ADR-007）
    dingtalk_approval_id VARCHAR(64),                           -- 钉钉审批单 ID
    submitted_by    VARCHAR(64) NOT NULL,
    submitted_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reviewed_by     VARCHAR(64),
    reviewed_at     TIMESTAMPTZ,
    review_comment  TEXT,
    applied_at      TIMESTAMPTZ
);

CREATE INDEX idx_change_tenant_api ON api_change_request(tenant_id, api_id, status);
```

### 2.11 Phase 3：计费相关（暂不启用，预留 schema）

```sql
-- ⚠️ Phase 3 启用，当前可创建表结构但不写入

CREATE TABLE billing_account (
    tenant_id       BIGINT NOT NULL,
    id              BIGSERIAL PRIMARY KEY,
    balance_cents   BIGINT NOT NULL DEFAULT 0,                  -- 余额（分）
    currency        VARCHAR(8) NOT NULL DEFAULT 'CNY',
    payment_method  JSONB,
    status          VARCHAR(20) NOT NULL DEFAULT 'active',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE subscription (
    tenant_id       BIGINT NOT NULL,
    id              BIGSERIAL PRIMARY KEY,
    plan_code       VARCHAR(64) NOT NULL,                       -- free/starter/pro/enterprise
    period_start    TIMESTAMPTZ NOT NULL,
    period_end      TIMESTAMPTZ NOT NULL,
    quota_included  JSONB NOT NULL,                             -- 套餐内含配额
    price_cents     BIGINT NOT NULL,
    status          VARCHAR(20) NOT NULL,
    auto_renew      BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 计费明细（T+1 从 ClickHouse 聚合）
CREATE TABLE billing_record (
    tenant_id       BIGINT NOT NULL,
    id              BIGSERIAL PRIMARY KEY,
    subscription_id BIGINT REFERENCES subscription(id),
    period_start    TIMESTAMPTZ NOT NULL,
    period_end      TIMESTAMPTZ NOT NULL,
    -- 用量明细
    call_count      BIGINT NOT NULL,
    token_count     BIGINT NOT NULL,                            -- AI 接口
    bandwidth_bytes BIGINT NOT NULL,
    -- 计费
    base_charge_cents   BIGINT NOT NULL,
    overage_charge_cents BIGINT NOT NULL,
    total_charge_cents  BIGINT NOT NULL,
    status          VARCHAR(20) NOT NULL DEFAULT 'pending',     -- pending/paid/disputed
    invoice_url     VARCHAR(512),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_billing_tenant_period ON billing_record(tenant_id, period_start DESC);
```

## 3. PostgreSQL RLS 策略

> 应用层 + RLS 双重隔离，详见 [11-multi-tenant.md §2](11-multi-tenant.md#2-隔离方式)。

```sql
-- 对所有元数据表启用 RLS
ALTER TABLE api_definition ENABLE ROW LEVEL SECURITY;
ALTER TABLE api_version ENABLE ROW LEVEL SECURITY;
ALTER TABLE app ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_api_key ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_api_grant ENABLE ROW LEVEL SECURITY;
ALTER TABLE task_instance ENABLE ROW LEVEL SECURITY;
ALTER TABLE retry_task ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE api_change_request ENABLE ROW LEVEL SECURITY;

-- 通用策略（每张表）
CREATE POLICY tenant_isolation ON api_definition
    USING (tenant_id = current_setting('app.current_tenant_id', true)::bigint);

-- 超级管理员 bypass（平台运维角色）
CREATE POLICY super_admin_bypass ON api_definition
    FOR ALL
    USING (current_setting('app.is_super_admin', true)::boolean);

-- 应用连接时设 session 变量
-- SET LOCAL app.current_tenant_id = '123';
-- SET LOCAL app.is_super_admin = 'false';
```

## 4. ClickHouse Schema

### 4.1 调用日志主表（含租户 + AI 字段）

```sql
CREATE TABLE api_call_log (
    -- 租户（多租户决策）
    tenant_id       UInt64,
    tenant_code     LowCardinality(String),
    -- 标识
    trace_id        String,
    parent_trace_id String DEFAULT '',
    span_id         String,
    api_id          UInt64,
    api_uuid        String,
    api_path        LowCardinality(String),
    api_method      LowCardinality(String),
    api_version     LowCardinality(String),
    api_mode        LowCardinality(String),
    -- 调用方
    app_id          UInt64,
    app_uuid        String,
    app_name        LowCardinality(String),
    caller_ip       IPv4,
    caller_type     LowCardinality(String),
    -- 环境
    env             LowCardinality(String),
    gateway_node    LowCardinality(String),
    -- 请求
    req_id          String,
    req_size        UInt32,
    req_header_keys Array(String),
    req_body_ref    String DEFAULT '',
    -- 响应
    http_status     UInt16,
    biz_code        Int32,
    resp_size       UInt32,
    resp_body_ref   String DEFAULT '',
    -- 性能
    latency_ms      UInt32,
    gateway_latency_ms UInt16,
    backend_latency_ms UInt16,
    -- AI 网关扩展
    is_streaming    UInt8,
    token_prompt    UInt32,
    token_completion UInt32,
    token_total     UInt32,
    ai_model        LowCardinality(String),
    -- 错误
    is_success      UInt8,
    is_timeout      UInt8,
    error_type      LowCardinality(String),
    error_msg       String,
    -- 重试
    is_retry        UInt8,
    retry_no        UInt8,
    -- 任务
    task_id         String DEFAULT '',
    -- 时间
    ts              DateTime,
    ts_hour         DateTime MATERIALIZED toStartOfHour(ts),
    ts_date         Date MATERIALIZED toDate(ts)
) ENGINE = MergeTree
PARTITION BY toYYYYMMDD(ts)
ORDER BY (ts_hour, tenant_id, api_id, app_id, trace_id)
TTL ts + INTERVAL 180 DAY
SETTINGS index_granularity = 8192;

ALTER TABLE api_call_log ADD INDEX idx_trace trace_id TYPE bloom_filter GRANULARITY 4;
ALTER TABLE api_call_log ADD INDEX idx_tenant_api (tenant_id, api_id) TYPE minmax GRANULARITY 4;
```

### 4.2 按租户聚合视图

```sql
CREATE MATERIALIZED VIEW api_call_stats_by_tenant
ENGINE = SummingMergeTree()
PARTITION BY toYYYYMMDD(ts_hour)
ORDER BY (ts_hour, tenant_id, api_id, app_id, env, http_status)
AS
SELECT
    ts_hour,
    tenant_id,
    api_id,
    app_id,
    env,
    http_status,
    biz_code,
    is_success,
    count() AS call_count,
    sum(latency_ms) AS total_latency_ms,
    max(latency_ms) AS max_latency_ms,
    sum(req_size) AS total_req_bytes,
    sum(resp_size) AS total_resp_bytes,
    sum(is_retry) AS retry_count,
    -- AI 字段聚合
    sum(token_total) AS total_tokens,
    sum(is_streaming) AS streaming_count
FROM api_call_log
GROUP BY ts_hour, tenant_id, api_id, app_id, env, http_status, biz_code, is_success;
```

### 4.3 Kafka Engine

```sql
CREATE TABLE api_call_log_kafka
ENGINE = Kafka(
    'kafka-1:9092,kafka-2:9092,kafka-3:9092',
    'api-call-events',
    'ch-consumer-group',
    'JSONEachRow'
);

CREATE MATERIALIZED VIEW api_call_log_consumer TO api_call_log AS
SELECT * FROM api_call_log_kafka;
```

## 5. Redis Key 规划（含租户前缀）

### 5.1 租户元数据（极热）
```
t:{tenant_id}:meta                         → JSON, TTL 30m
```

### 5.2 接口元数据
```
t:{tenant_id}:api:{api_uuid}               → JSON, TTL 1h
t:{tenant_id}:api:path:{path}:{method}     → api_uuid, TTL 1h
t:{tenant_id}:api:versions:{api_uuid}      → Set, TTL 1h
```

### 5.3 应用与 Key
```
t:{tenant_id}:app:{app_uuid}               → JSON, TTL 30m
t:{tenant_id}:app:key:{key_id}             → JSON, TTL 10m
t:{tenant_id}:app:grants:{app_uuid}        → Set, TTL 10m
```

### 5.4 限流计数（三层配额）
```
t:{tenant_id}:rate:{api_id}:{app_id}:{minute_slot}    # 应用 × API
t:{tenant_id}:rate:app:{app_id}:{minute_slot}          # 应用总
t:{tenant_id}:rate:tenant:{minute_slot}                # 租户总
t:{tenant_id}:rate:global:{minute_slot}                # （可选）租户级全局限流
```

### 5.5 延迟队列
```
t:{tenant_id}:retry:delayed               → ZSet
t:{tenant_id}:retry:processing            → Set
```

### 5.6 分布式锁
```
lock:t:{tenant_id}:api:{api_uuid}:publish  → String, TTL 30s
```

## 6. MinIO 对象存储路径

```
apihub/
├── call-bodies/
│   └── {env}/{tenant_id}/{yyyy}/{mm}/{dd}/{trace_id}.req.json
│                                              .resp.json
│                                              .error.txt
├── sdk-packages/
│   └── {tenant_id}/{api_uuid}/{version}/{lang}/latest.{ext}
├── workflow-artifacts/
│   └── {tenant_id}/{workflow_id}/...
├── audit-archive/
│   └── {yyyy}/{mm}/tenant-{tenant_id}-{yyyy}-{mm}.parquet
└── billing-invoices/        (Phase 3)
    └── {tenant_id}/{yyyy}-{mm}.pdf
```

## 7. Kafka 消息规范

### 7.1 通用 Header

所有 Kafka 消息必须带：
```
header: tenant_id     (string, 必填)
header: trace_id      (string, 必填)
header: event_type    (string)
header: event_version (string)
header: timestamp     (long)
```

### 7.2 主要 topic

| topic | 分区 | 分区键 | 用途 |
|-------|------|--------|------|
| `api-call-events` | 64 | `{tenant_id}:{trace_id}` | 调用日志 |
| `task-requests` | 32 | `{tenant_id}:{api_id}` | 异步任务派发 |
| `task-failures` | 16 | `{tenant_id}:{retry_task_id}` | 失败投递 |
| `audit-events` | 8 | `{tenant_id}:{actor_id}` | 审计 |
| `notification-events` | 8 | `{tenant_id}:{recipient}` | 通知（钉钉等） |
| `billing-events` | 4 | `{tenant_id}` | Phase 3 计费 |

## 8. etcd（APISIX 配置）

```
/apisix/routes/                路由（含 tenant 标签）
/apisix/consumers/             调用方（API Key 关联，含 tenant_id 属性）
/apisix/global_rules/          全局规则
/apisix/ssl/                   SSL 证书
```

调用方带 `tenant_id` 属性，便于 APISIX 在路由时注入 `X-Tenant-Id` Header。

## 9. Jaeger Schema

业务侧只需关心：
- `trace_id` 贯穿全程
- Span tag 加 `tenant.id`、`api.id`、`app.id`、`ai.model`（如有）

业务服务通过 OpenTelemetry SDK 自动注入这些 tag。

## 10. 数据生命周期

| 数据 | 热数据 | 温数据 | 冷归档 | 备注 |
|------|--------|--------|--------|------|
| 调用日志 (CH) | 7 天 SSD | 30 天 HDD | 180 天后删 | 等保要求 6 月+，存档 |
| 任务实例 (PG) | 当月 | 12 个月 | 转 OSS Parquet |  |
| 审计日志 (PG) | 6 个月在线 | 12 个月温 | OSS 永久 | **等保三级要求 6 月+** |
| 错误堆栈 (MinIO) | 30 天 | 180 天 | 删 |  |
| SDK 包 | 永久 | - | - |  |
| 链路 Span | 7 天 | 14 天 | 删 |  |
| 计费记录（Phase 3） | 当年 | 5 年 | OSS 永久 | 财务合规 |

## 11. 数据安全（等保 2.0 三级要求）

| 数据类型 | 加密 / 脱敏 | 备注 |
|---------|------------|------|
| 传输中 | TLS 1.3 | 全链路 HTTPS |
| API Key secret | SHA256 hash | 明文永不落库 |
| Webhook secret | KMS 加密 |  |
| 调用方 PII（IP） | 部分脱敏展示 | 原始入库 |
| 请求 body | 按接口配置脱敏 | password 字段移除等 |
| 数据库备份 | KMS 加密 | RDS 自带 |
| 跨环境数据 | 物理隔离 | dev/staging/prod 不互通 |
| 数据库审计 | RDS SQL 审计开启 | 等保三级要求 |
| 跨租户访问 | RLS + 应用层 + 审计 | 详见 11-multi-tenant.md |
| 敏感字段 | 字段级加密（如适用） | 身份证号、银行卡号 |
