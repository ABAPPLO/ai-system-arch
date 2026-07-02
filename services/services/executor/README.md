# executor

> 异步任务执行器 —— 消费 Kafka `task-requests`，调业务后端，回写 PG task 表。
> 详见 [docs/03-services.md §3.3](../../../docs/03-services.md) + [docs/05-core-flows.md §3](../../../docs/05-core-flows.md)。

## 架构（worker pattern）

```
                  ┌── FastAPI lifespan ──┐
                  │                      │
进程启动 → init PG / Redis / Kafka producer / httpx client
                  ↓
       reset_stale_running (启动时清理上次崩溃残留)
                  ↓
       TaskConsumer.start() → 后台 asyncio task
                  ↓
循环：拽 Kafka 消息 → process_task → commit offset
                  ↓
优雅退出：SIGTERM → consumer.stop() → 等 worker 处理完当前消息（≤30s）
```

HTTP server（端口 8003）只暴露 `/health/*` 给 k8s probe，**不接受业务流量**。

## 状态机

```
dispatcher:                 executor:
INSERT status='pending'  →  mark_running (pending→running，原子 UPDATE WHERE status='pending')
                            ↓ won=True
                            POST backend_url
                            ↓
                            2xx → mark_succeeded
                            4xx/5xx → mark_failed (error_code=backend_http_XXX)
                            timeout → mark_failed (status=timeout)
                            conn err → mark_failed (error_code=backend_unreachable)
                            ↓
                            emit task-status event（notifier 消费）
```

幂等保证：`mark_running` 用 `WHERE status='pending'` 守门，重复消费（at-least-once）时 `won=False` → 直接跳过。

## 关键设计

### 1. at-least-once + 幂等

Kafka commit 在 task 处理之后。崩溃 → 已处理但未 commit → 重启后重投 → `mark_running` 看到 `running`/`succeeded` 直接跳过。这是 at-least-once + 业务幂等组合保证 exactly-once 效果。

### 2. 单消息异常不杀 worker

```python
async for msg in consumer:
    try:
        await self._handle(msg)
    except Exception:
        log.exception(...)   # 不能让单条毒消息拖垮整个 worker
    await commit()
```

失败的已经写 PG failed，再投也会被幂等跳过，所以**也 commit**（避免反复重投）。

### 3. reset_stale_running

启动时清理上次崩溃留下的 `running` 任务（超过 N 秒未结束）：
```sql
UPDATE task SET status='pending', started_at=NULL
WHERE status='running' AND started_at < NOW() - interval '600 seconds'
```

让上一轮崩溃的任务在下次被 dispatcher 重新轮询 / 用户手动重试时能继续。

### 4. 跨租户写

executor 是后台 worker，没有入站 HTTP → 没自动 TenantContext。所有 PG 操作走 `admin_db_session`（绕 RLS），靠 task 表自己的 `tenant_id` 字段做语义隔离（task 表本身有 RLS，但 admin session 绕过）。

⚠️ 这意味着 executor 写的 task 行对**所有租户可见**（task.tenant_id 仍是任务原始租户）。查询时正常走 RLS，租户 A 看不到租户 B 的任务。

### 5. Kafka header 自动注入

`process_task` 临时设 TenantContext → `kafka.emit("task-status", ...)` 自动带 `tenant_id` header。emit 完清掉，避免上下文串。

## 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health/live`  | k8s liveness probe |
| GET | `/health/ready` | k8s readiness probe（consumer 起来 + DB pool 建好才 ready） |
| GET | `/metrics`      | 占位（真版用 prometheus_client） |

## 本地开发

```bash
make install
make dev-up              # 起 PG/Redis/Kafka
make run-executor        # uvicorn executor.main:app --port 8003
```

手动投一条消息（需要 Kafka 命令行工具）：
```bash
kafka-console-producer.sh --bootstrap-server localhost:9094 \
  --topic task-requests \
  --property "key.separator=|" \
  --property "key.serializer=org.apache.kafka.common.serialization.StringSerializer" \
  --header "tenant_id=t_demo" --header "app_id=app_demo" --header "request_id=req_test"

# 然后输入：
# task_test12345|{"task_id":"task_test12345","api_id":"api_users","api_version_id":"ver_1","backend_url":"http://localhost:9999/hook","payload":"{}","timeout_seconds":5}
```

## 测试

```bash
cd services/services/executor
pytest tests/ -v
# 23 tests, all pass
```

覆盖：
- `test_processor.py`（10）—— 2xx/4xx/5xx/timeout/conn-refused + 幂等跳过 + headers/payload 传递
- `test_repository.py`（9）—— mark_running/succeeded/failed SQL + timeout→status 映射 + reset_stale_running
- `test_consumer.py`（4）—— 消息解析 + header 回填 + commit per message + 异常不杀 loop

mock 策略：httpx client 用 `MagicMock` + `AsyncMock`，admin_db_session 用 `asynccontextmanager` 包 fake conn，Kafka consumer 用 _FakeConsumer async iterator。

## 性能预算（prod）

- 10 副本起步（HPA 基于 Kafka consumer lag）
- 单副本 4000m CPU / 4Gi 内存（同时调多个 backend）
- 默认 backend timeout 30s，单消息处理 P99 < 35s
- 单副本并发：1 个 worker task（横向扩展靠副本数；单副本并发靠后续加 `asyncio.gather`）
