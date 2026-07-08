-- ============================================================
-- 业务账号 apihub_app —— NOSUPERUSER / NOBYPASSRLS
--
-- 为什么需要这个账号：
--   POSTGRES_USER（apihub）默认是 superuser + BYPASSRLS，
--   superuser 永远绕过 RLS（FORCE ROW LEVEL SECURITY 也只对 owner 生效）。
--   业务服务必须连这个账号才能让租户隔离真正生效。
--
-- 职责划分：
--   apihub        —— DDL/migration owner（superuser，仅 init/migrate 用）
--   apihub_app    —— 业务流量账号（NOSUPERUSER NOBYPASSRLS）
--                    所有 service（api-registry/admin-bff/retry-svc/...）用这个
--
-- 密码：dev 默认 apihub_app_dev_pwd；要改密码：
--   ALTER ROLE apihub_app PASSWORD 'new_pwd';
-- ============================================================

-- 1. 创建业务账号（dev 默认密码写死；生产用 K8s Secret 单独建账号）
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'apihub_app') THEN
        CREATE ROLE apihub_app
            LOGIN
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            NOREPLICATION
            NOBYPASSRLS
            PASSWORD 'apihub_app_dev_pwd';
    END IF;
END $$;

-- 2. 默认权限：以后在 public schema 建的表/序列，自动给 apihub_app 授权
--    这样 01-schema.sql / 03-phase2.sql 建表时权限会自动到位
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO apihub_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO apihub_app;

-- 3. schema 本身的 USAGE（必须，否则连不上）
GRANT USAGE ON SCHEMA public TO apihub_app;
