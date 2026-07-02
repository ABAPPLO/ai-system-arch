-- ClickHouse 调用日志 schema —— 详见 docs/04-data-model.md §4
-- 注意：ClickHouse 不做 RLS（无 tenant 隔离），靠查询 SQL WHERE tenant_id 过滤

CREATE DATABASE IF NOT EXISTS apihub;

USE apihub;

-- ============================================================
-- 调用日志（Kafka Engine 直读 + Materialized View 转存）
-- ============================================================

-- Kafka source 表（直连 Kafka topic 消费）
CREATE TABLE IF NOT EXISTS api_call_events_src (
    ts                  DateTime64(3) DEFAULT now64(),
    tenant_id           String,
    tenant_type         LowCardinality(String),
    app_id              String,
    api_id              String,
    api_version_id      String,
    trace_id            String,
    request_id          String,
    method              LowCardinality(String),
    path                String,
    status_code         UInt16,
    is_success          UInt8,
    latency_ms          UInt32,
    request_size        UInt32,
    response_size       UInt32,
    error_code          LowCardinality(String) DEFAULT '',
    error_msg           String DEFAULT '',
    user_agent          String DEFAULT '',
    client_ip           IPv4 DEFAULT toIPv4('0.0.0.0'),
    backend_type        LowCardinality(String) DEFAULT 'http',
    backend_latency_ms  UInt32 DEFAULT 0,
    -- AI 字段
    ai_model            LowCardinality(String) DEFAULT '',
    ai_streaming        UInt8 DEFAULT 0,
    token_prompt        UInt32 DEFAULT 0,
    token_completion    UInt32 DEFAULT 0,
    token_total         UInt32 DEFAULT 0
)
ENGINE = Kafka
SETTINGS kafka_broker_list = 'kafka:9092',
         kafka_topic_list = 'api-call-events',
         kafka_group_name = 'clickhouse-sink-api-call-events',
         kafka_format = 'JSONEachRow',
         kafka_handle_error_mode = 'stream';

-- 实存表（MergeTree，按 tenant + 日分区）
CREATE TABLE IF NOT EXISTS api_call_log (
    ts                  DateTime64(3),
    tenant_id           String,
    tenant_type         LowCardinality(String),
    app_id              String,
    api_id              String,
    api_version_id      String,
    trace_id            String,
    request_id          String,
    method              LowCardinality(String),
    path                String,
    status_code         UInt16,
    is_success          UInt8,
    latency_ms          UInt32,
    request_size        UInt32,
    response_size       UInt32,
    error_code          LowCardinality(String),
    error_msg           String,
    user_agent          String,
    client_ip           IPv4,
    backend_type        LowCardinality(String),
    backend_latency_ms  UInt32,
    ai_model            LowCardinality(String),
    ai_streaming        UInt8,
    token_prompt        UInt32,
    token_completion    UInt32,
    token_total         UInt32,
    error_stack_ref     String DEFAULT ''   -- MinIO 路径（避免大堆栈塞 CH）
)
ENGINE = MergeTree
PARTITION BY (toYYYYMM(ts), tenant_id)
ORDER BY (tenant_id, api_id, ts)
SETTINGS index_granularity = 8192;

-- 物化视图：Kafka → MergeTree
CREATE MATERIALIZED VIEW IF NOT EXISTS api_call_log_mv TO api_call_log AS
SELECT * FROM api_call_events_src;

-- ============================================================
-- 预聚合：每小时 QPS / 错误率 / 延迟（查大盘 < 1s）
-- ============================================================

CREATE TABLE IF NOT EXISTS api_call_stats_hourly (
    hour            DateTime,
    tenant_id       String,
    api_id          String,
    app_id          String,
    calls           UInt64,
    errors          UInt64,
    success_rate    Float64,
    p50_ms          UInt32,
    p95_ms          UInt32,
    p99_ms          UInt32,
    qps             Float64,
    tokens_total    UInt64
)
ENGINE = SummingMergeTree
PARTITION BY toYYYYMMDD(hour)
ORDER BY (hour, tenant_id, api_id, app_id);

-- ============================================================
-- 测试数据（手动插入若干行验证）
-- ============================================================
INSERT INTO api_call_log (
    ts, tenant_id, tenant_type, app_id, api_id, api_version_id,
    trace_id, request_id, method, path, status_code, is_success,
    latency_ms, request_size, response_size, error_code, error_msg,
    user_agent, client_ip, backend_type, backend_latency_ms,
    ai_model, ai_streaming, token_prompt, token_completion, token_total
)
VALUES
    (now() - INTERVAL 1 HOUR, 'tenant_a', 'internal', 'app_trading', 'api_demo_a', 'ver_demo_a_v1',
     'trc_001', 'req_001', 'GET', '/v1/users/u1', 200, 1,
     45, 100, 350, '', '', 'curl/8', toIPv4('10.0.10.1'), 'http', 40,
     '', 0, 0, 0, 0),
    (now() - INTERVAL 30 MINUTE, 'tenant_a', 'internal', 'app_trading', 'api_demo_llm', 'ver_demo_llm_v1',
     'trc_002', 'req_002', 'POST', '/v1/llm/chat', 200, 1,
     1200, 200, 1500, '', '', 'sdk-py/1.0', toIPv4('10.0.10.2'), 'ai_model', 1150,
     'gpt-4o-mini', 1, 120, 80, 200),
    (now() - INTERVAL 10 MINUTE, 'tenant_b', 'internal', 'app_risk', 'api_demo_b', 'ver_demo_b_v1',
     'trc_003', 'req_003', 'GET', '/v1/orders/o1', 500, 0,
     1500, 80, 0, 'INTERNAL', 'backend down', 'httpx/0.27', toIPv4('10.0.10.3'), 'http', 1490,
     '', 0, 0, 0, 0);

-- 验证查询：
--   SELECT tenant_id, count(), avg(latency_ms) FROM apihub.api_call_log GROUP BY tenant_id;
--   SELECT * FROM apihub.api_call_log WHERE is_success = 0;
