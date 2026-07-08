-- ============================================================
-- 收尾授权 —— 对已建好的所有表/序列显式授权给 apihub_app
--
-- 背景：00-roles.sql 里的 ALTER DEFAULT PRIVILEGES 只对【未来】建的表生效。
-- 已经在 01-schema.sql / 02-seed.sql / 03-phase2.sql 里建的表需要显式 GRANT。
--
-- 这个文件命名为 99- 让它在最后跑。
-- ============================================================

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO apihub_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO apihub_app;

-- 验证（开发期自检）：
-- \du apihub_app              -> 应该看到 NOSUPERUSER NOBYPASSRLS
-- SET role apihub_app;
-- SET app.tenant_id = 'tenant_a';
-- SELECT count(*) FROM api;   -> 应该只见 tenant_a 的数据
-- RESET role;
