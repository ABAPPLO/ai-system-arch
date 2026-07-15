-- R1a §2.7: retry_task 幂等 —— 同一 task_instance_id 同时只允许一个活跃 retry_task。
-- 上次重试已 dead/succeeded/ignored 后再次失败可建新行（partial unique 只约束活跃集）。
-- ON CONFLICT DO NOTHING（无列推断）会把此 partial unique index 作为 conflict arbiter，
-- Kafka at-least-once 重投时 INSERT 静默跳过，由 repository.create_retry_task 返回 0 表去重。
DROP INDEX IF EXISTS idx_retry_task_active_dedup;
CREATE UNIQUE INDEX IF NOT EXISTS idx_retry_task_active_dedup
  ON retry_task(task_instance_id) WHERE status IN ('pending', 'running');
