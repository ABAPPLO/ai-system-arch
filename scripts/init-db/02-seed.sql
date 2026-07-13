-- ============================================================
-- 种子数据：2 个租户 + 用户 + 应用 + APIKey + 示例 API
-- APIKey 明文：
--   tenant_a 应用:  ak_test_a_xxxxxxxx  (hash 见下)
--   tenant_b 应用:  ak_test_b_xxxxxxxx
--
-- 用 Python 算 sha256（key_hash 字段需要明文 hash）：
--   python3 -c "import hashlib; print(hashlib.sha256(b'ak_test_a_demo001').hexdigest())"
-- ============================================================

BEGIN;

-- 租户
INSERT INTO tenant (id, name, slug, type, status, tier, metadata)
VALUES
    ('tenant_a', '内部业务-A', 'internal-a', 'internal', 'active', 'premium',
     '{"dept": "trading", "cost_center": "CC-001"}'::jsonb),
    ('tenant_b', '内部业务-B', 'internal-b', 'internal', 'active', 'standard',
     '{"dept": "risk"}'::jsonb),
    ('tenant_ext_1', '外部合作方-X', 'ext-x', 'external', 'active', 'free',
     '{}'::jsonb),
    -- 外部开发者自助注册的默认租户（身份地基 Phase 3）：注册成功后 verify-email
    -- 即加入此租户为 developer。type=external，free 档。
    ('external-public', '外部公共（自助注册）', 'external-public', 'external',
     'active', 'free', '{}'::jsonb)
ON CONFLICT (id) DO NOTHING;

-- 用户（password_hash 是 bcrypt 占位，本地登录用，不要在 prod 用）
INSERT INTO user_account (id, email, phone, password_hash, name, verification_level, status)
VALUES
    ('user_alice', 'alice@apihub.local', '13800000001',
     '$2b$12$placeholderplaceholderplaceholderplaceholderplaceholderplacehold',
     'Alice (A 管理员)', 'email_phone', 'active'),
    ('user_bob', 'bob@apihub.local', '13800000002',
     '$2b$12$placeholderplaceholderplaceholderplaceholderplaceholderplacehold',
     'Bob (B 开发者)', 'email', 'active'),
    ('user_carol', 'carol@ext-x.com', '13900000003',
     '$2b$12$placeholderplaceholderplaceholderplaceholderplaceholderplacehold',
     'Carol (外部调用方)', 'email', 'active')
ON CONFLICT (id) DO NOTHING;

-- 成员关系
INSERT INTO tenant_member (id, tenant_id, user_id, role)
VALUES
    ('tm_1', 'tenant_a', 'user_alice', 'owner'),
    ('tm_2', 'tenant_b', 'user_bob', 'developer'),
    ('tm_3', 'tenant_ext_1', 'user_carol', 'owner')
ON CONFLICT (id) DO NOTHING;

-- 应用
INSERT INTO app (id, tenant_id, name, type, status, quota_tier, metadata)
VALUES
    ('app_trading', 'tenant_a', '交易系统', 'server', 'active', 'premium', '{}'::jsonb),
    ('app_risk',    'tenant_b', '风控系统', 'server', 'active', 'standard', '{}'::jsonb),
    ('app_ext_x',   'tenant_ext_1', '外部调用-X', 'server', 'active', 'free', '{}'::jsonb)
ON CONFLICT (id) DO NOTHING;

-- API Key
-- 明文（本地用，prod 自动生成）：
--   ak_test_a_demo001   →  tenant_a / app_trading
--   ak_test_b_demo001   →  tenant_b / app_risk
--   ak_test_ext_x_demo  →  tenant_ext_1 / app_ext_x
-- 计算方式：python3 -c "import hashlib; print(hashlib.sha256(b'ak_test_a_demo001').hexdigest())"
INSERT INTO api_key (id, tenant_id, app_id, key_prefix, key_hash, name, scopes, status)
VALUES
    ('key_a1', 'tenant_a', 'app_trading', 'ak_test_',
     'c718af9f9532c71b958db43927477151b485bf23cb162a6f0a4920882c9b68f3',
     'dev key', ARRAY['*'], 'active'),
    ('key_b1', 'tenant_b', 'app_risk', 'ak_test_',
     '80b55143496adcf937fe8e9844c3efda2db065662f3b858ff3eb50554e68fa7d',
     'dev key', ARRAY['*'], 'active'),
    ('key_ext', 'tenant_ext_1', 'app_ext_x', 'ak_test_',
     '07047767166e23a814a43a405ba0f9acd79dde51f7019d7b04b702112c37d137',
     'dev key', ARRAY['read'], 'active')
ON CONFLICT (id) DO NOTHING;

-- 示例接口（每个租户一个）
-- 给 tenant_a 一个 demo API
SET app.tenant_id = 'tenant_a';
INSERT INTO api (id, tenant_id, name, description, category, base_path, tags, status, visibility)
VALUES
    ('api_demo_a', 'tenant_a', '用户查询-A', '示例：根据 user_id 查询用户',
     'user-service', '/user-service', ARRAY['user', 'query'], 'published', 'tenant')
ON CONFLICT (id) DO NOTHING;

INSERT INTO api_version (
    id, tenant_id, api_id, version, backend_type, backend_url, method, path,
    request_schema, response_schema, masking, rate_limit, retry_policy, cache_policy,
    auth_policy, status, published_at
)
VALUES
    ('ver_demo_a_v1', 'tenant_a', 'api_demo_a', 'v1', 'http',
     'http://example.internal/v1/users/{user_id}', 'GET', '/v1/users/{user_id}',
     '{"type":"object","properties":{"user_id":{"type":"string"}},"required":["user_id"]}'::jsonb,
     '{"type":"object","properties":{"name":{"type":"string"}}}'::jsonb,
     '{"response":[{"field":"phone","action":"mask"}]}'::jsonb,
     '{"count":1000,"window_seconds":60}'::jsonb,
     '{"max_attempts":3,"backoff_seconds":1,"backoff_multiplier":2}'::jsonb,
     '{"enabled":true,"ttl_seconds":60,"vary_by":["user_id"]}'::jsonb,
     '{"methods":["api_key"],"scopes":["user:read"]}'::jsonb,
     'published', NOW())
ON CONFLICT (id) DO NOTHING;

-- tenant_b
SET app.tenant_id = 'tenant_b';
INSERT INTO api (id, tenant_id, name, description, category, base_path, tags, status, visibility)
VALUES
    ('api_demo_b', 'tenant_b', '订单查询-B', '示例：订单查询',
     'order-service', '/order-service', ARRAY['order'], 'published', 'tenant')
ON CONFLICT (id) DO NOTHING;

INSERT INTO api_version (id, tenant_id, api_id, version, backend_type, backend_url, method, path, status)
VALUES
    ('ver_demo_b_v1', 'tenant_b', 'api_demo_b', 'v1', 'http',
     'http://example.internal/v1/orders/{order_id}', 'GET', '/v1/orders/{order_id}',
     'published')
ON CONFLICT (id) DO NOTHING;

-- AI 接口示例（tenant_a）
SET app.tenant_id = 'tenant_a';
INSERT INTO api (id, tenant_id, name, description, category, base_path, tags, status, visibility)
VALUES
    ('api_demo_llm', 'tenant_a', 'LLM 对话', '示例：gpt-4o 流式对话',
     'ai-service', '/ai-service', ARRAY['ai', 'llm', 'sse'], 'published', 'tenant')
ON CONFLICT (id) DO NOTHING;

INSERT INTO api_version (
    id, tenant_id, api_id, version, backend_type, backend_url, method, path,
    ai_model, ai_streaming, ai_params, status
)
VALUES
    ('ver_demo_llm_v1', 'tenant_a', 'api_demo_llm', 'v1', 'ai_model',
     'http://llm-gateway.internal/v1/chat/completions', 'POST', '/v1/llm/chat',
     'gpt-4o-mini', true,
     '{"temperature":0.7,"max_tokens":4096}'::jsonb,
     'published')
ON CONFLICT (id) DO NOTHING;

RESET app.tenant_id;

COMMIT;

-- ============================================================
-- 自检：用 RLS 看到什么
-- ============================================================
-- 模拟 tenant_a 的请求：
--   BEGIN; SET LOCAL app.tenant_id = 'tenant_a';
--   SELECT id, name FROM api;
--   COMMIT;
-- 应只看到 api_demo_a + api_demo_llm，看不到 api_demo_b

-- 模拟超管：
--   BEGIN;
--   SET LOCAL app.tenant_id = '';
--   SET LOCAL app.is_platform_admin = 'true';
--   SELECT tenant_id, count(*) FROM api GROUP BY tenant_id;
--   COMMIT;
-- 应看到全部租户
