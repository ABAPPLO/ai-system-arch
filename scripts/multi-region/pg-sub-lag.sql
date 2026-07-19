-- 订阅复制 lag（秒）。在订阅端执行。origin=none 下用 pg_stat_subscription。
-- 用法：psql "$PG_DSN_BJ" -f scripts/multi-region/pg-sub-lag.sql
-- 返回每条 subscription 的 received_lag / latest_end_lag（interval），NULL=已追平。
SELECT
  subname,
  received_lag,
  latest_end_lag,
  last_msg_receipt_time,
  NOW() - last_msg_receipt_time AS since_recv
FROM pg_stat_subscription
ORDER BY subname;
