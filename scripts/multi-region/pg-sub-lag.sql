-- 订阅复制 lag。在订阅端执行。PG16 的 pg_stat_subscription 无 received_lag/latest_end_lag（PG17+ 才有），
-- 用 NOW() - latest_end_time 作 lag 代理（latest_end_time = 最近收到的 WAL end 时间；NULL=未收到/已追平无新数据）。
-- 用法：psql "$PG_DSN_BJ" -f scripts/multi-region/pg-sub-lag.sql
SELECT
  subname,
  received_lsn,
  latest_end_lsn,
  last_msg_send_time,
  last_msg_receipt_time,
  latest_end_time,
  NOW() - latest_end_time AS latest_end_lag,
  NOW() - last_msg_receipt_time AS since_recv
FROM pg_stat_subscription
ORDER BY subname;
