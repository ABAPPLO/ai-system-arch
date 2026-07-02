"""executor —— 异步任务执行器。

职责：
1. 消费 Kafka topic `task-requests`（消费组 `executor`）
2. 调用业务后端（HTTP POST，timeout 受控）
3. 把状态机推进结果写回 PG task 表（pending → running → succeeded/failed/timeout）
4. 状态变更推 Kafka `task-status`（observability + notifier 消费）

详见 docs/03-services.md §3.3 + docs/05-core-flows.md §3。
"""
