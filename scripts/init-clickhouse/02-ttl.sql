-- 02 · 调用日志 TTL（docs/04-data-model.md 承诺 `TTL toDateTime(ts) + INTERVAL 180 DAY`，此前缺失）
--
-- 01-schema.sql 的 CREATE TABLE IF NOT EXISTS 只对新建表生效；存量库需本迁移补 TTL。
-- 幂等：MODIFY TTL 重复执行无害（CH 以最后一次为准）。
-- 注意：TTL 生效需要 merge 事件；大表首次回填清理会分批在后台 merge 完成。

USE apihub;

ALTER TABLE api_call_log      MODIFY TTL toDateTime(ts) + INTERVAL 180 DAY;
ALTER TABLE api_call_stats_hourly MODIFY TTL hour + INTERVAL 730 DAY;

-- 存量库手工执行：docker exec -i apihub-clickhouse clickhouse-client \
--   --multiquery < scripts/init-clickhouse/02-ttl.sql
-- 归档注意：180 天后明细自动删除。如需更长留存，先走 admin 的 audit OSS 归档
-- （archive_before 导出 JSONL.gz 到 OSS），再让 TTL 收尾。
