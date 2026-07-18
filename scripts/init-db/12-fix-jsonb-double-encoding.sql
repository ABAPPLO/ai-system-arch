-- R2e Task 5: 修 jsonb double-encoding 历史坏数据。
--
-- 背景：admin/repository.record / tenant/repository.create_tenant/set_quota 以及
-- db._write_admin_audit 早期版本传 json.dumps(x)::jsonb，生产 pool 的 jsonb codec
-- （encoder=json.dumps）会再次 JSON-encode 该字符串 → PG 存 jsonb 类型为 `string`
-- 而非 `object` → `detail->>'...'` / `metadata->'quota'->>'day_limit'` 返回 NULL。
-- 代码层已在 R2e Task 5 修复（codec 统一 default=str + 直传 dict）；本脚本修历史数据。
--
-- 幂等：只处理 jsonb_typeof = 'string' 的行；正常行（object/array 等）不动。
-- 重跑安全：字符串解除一层 JSON 引号编码后，若内部仍是合法 JSON 对象则落地为 object；
--           已修复的行 jsonb_typeof != 'string'，WHERE 直接跳过。
-- 注意：apply-db 须以 owner `apihub` 执行（见 scripts/k8s/apply-db.sh），且本脚本
--       不带 BEGIN/COMMIT —— 与 11-notification-channels.sql 自带事务的设计保持
--       互斥，apply-db 不加 --single-transaction，每条 UPDATE 各自原子提交。
UPDATE audit_log SET detail   = (detail::text)::jsonb   WHERE jsonb_typeof(detail)   = 'string';
UPDATE tenant    SET metadata = (metadata::text)::jsonb WHERE jsonb_typeof(metadata) = 'string';
UPDATE app       SET metadata = (metadata::text)::jsonb WHERE jsonb_typeof(metadata) = 'string';
