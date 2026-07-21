-- ============================================================
-- 14-hmac-secret.sql — R2e auth HMAC: 签名密钥列
--
-- 背景：R2e 引入 HMAC 签名鉴权（opt-in），需要：
--   - api_key.hmac_secret_encrypted: 每把 key 可选的 HMAC 签名密钥
--     （NULL = 该 key 仅支持 bearer，不改既有行为）。
--   - webhook_subscription.secret_encrypted: outbound webhook 签名密钥
--     的加密存储（AES-GCM b64）。存量明文 `secret` 列由
--     14-backfill-webhook-secret.py 加密回填 + SET secret=NULL scrub，
--     本脚本不 DROP，避免 ADD+DROP ordering hazard（见 task brief）。
--
-- 幂等：ADD COLUMN IF NOT EXISTS + COMMENT ON COLUMN（重跑安全）。
-- 与 12/13 一致：不带 BEGIN/COMMIT，apply-db 逐条原子提交，避免
-- --single-transaction（11-*.sql 内嵌 BEGIN/COMMIT，见 R2b memory）。
--
-- RLS：api_key/webhook_subscription 已在 01-schema/06-notification
-- ENABLE+FORCE，新列自动受既有 tenant_isolation policy 保护，无需新 policy。
-- GRANT：apihub_app 已有 SELECT/INSERT/UPDATE on 两表，新列自动覆盖。
-- ============================================================

-- api_key.hmac_secret_encrypted: opt-in HMAC 签名密钥（AES-GCM b64）
ALTER TABLE api_key ADD COLUMN IF NOT EXISTS hmac_secret_encrypted text;
COMMENT ON COLUMN api_key.hmac_secret_encrypted IS
  'AESGCM-encrypted HMAC signing secret (b64). NULL = key not enrolled for HMAC signing.';

-- webhook_subscription.secret_encrypted: outbound webhook 签名密钥（AES-GCM b64）
ALTER TABLE webhook_subscription ADD COLUMN IF NOT EXISTS secret_encrypted text;
COMMENT ON COLUMN webhook_subscription.secret_encrypted IS
  'AESGCM-encrypted outbound webhook signing secret (b64). NULL = no signing.';
