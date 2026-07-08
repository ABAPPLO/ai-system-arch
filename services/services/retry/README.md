# retry-svc

> 失败调用重试服务 —— Kafka `task-failures` 消费 + Redis ZSet 延迟队列 + executor 回调 + 死信处理。
> 详见 [docs/03-services.md §3.7](../../../docs/03-services.md)。

## 架构

```
executor (failed) ──→ Kafka (task-failures) ──→ retry-svc consumer
                                                       │
                                                       ↓
                                                PG retry_task (status=pending)
                                                       │
                                                       ↓
                                  Redis ZSet t:{tid}:retry:delayed (score = due_ts)
                                                       │
                                                       ↓
                                              retry-svc worker (1s 轮询)
                                                       │
                                                       ↓
                                       pop_due → POST executor /v1/internal/retry
                                                       │
                                          ┌────────────┴────────────┐
                                          ↓                         ↓
                                       200/2xx                    4xx/5xx/timeout
                                          │                         │
                                  mark_succeeded            retry_count+1 < max?
                                                                  │
                                                          ┌───────┴────────┐
                                                          ↓                ↓
                                                         yes              no
                                                          │                │
                                                  reschedule         dead_letter
                                                  (push ZSet)        (status=dead)
```

## Phase 2 范围

| 功能 | 状态 |
|------|------|
| Kafka task-failures 消费 + 写 PG retry_task | ✅ |
| Redis ZSet 延迟队列（LUA 原子 pop + processing） | ✅ |
| 指数退避（exponential + ±25% jitter） | ✅ |
| Worker 轮询 + executor 回调 | ✅ |
| 超过 max_attempts 进死信 | ✅ |
| GET /v1/retry/failed / stats / {id} | ✅ |
| POST /v1/retry/{id}/trigger / ignore | ✅ |
| Dashboard UI（失败任务后台） | ⏳ 前端 |
| HPA based on Kafka consumer lag | ⏳ SRE |

## 关键设计

### 1. 包名 `retry_svc` 不是 `retry`

Python stdlib 有 `trace.py` 模块（已踩坑，trace-svc 同样问题）。直接 `import retry`
会被 stdlib 拦截，必须用 `retry_svc`。

### 2. 写 PG 走 admin_db_session（无 TenantContext）

和 executor 一样，retry-svc 的 consumer / worker 是后台进程，没有 HTTP 入口 →
没有自动 TenantContext。所有写操作走 `db.admin_db_session()`，显式带 `tenant_id`
字段。RLS 兜底（policy 已配 `tenant_id = current_setting('app.tenant_id')`）。

读操作（HTTP route）走 `db.db_session()`，自动 `SET LOCAL app.tenant_id`。

### 3. Redis ZSet 跨租户操作

`apihub_core.redis.t_get/t_set` 有 tenant 前缀封装，但 worker 跨多租户轮询 →
直接用 `raw_client()` 拼完整 key：

```python
# delay_queue.py
key = f"t:{tenant_id}:retry:delayed"
await client.zadd(key, {str(retry_task_id): due_ts})
```

### 4. ZSet 原子 pop（LUA）

ZRANGEBYSCORE + ZREM + SADD(processing) 必须原子（避免多 worker 抢同一条）：

```python
# delay_queue.py - pop_due
lua = """
    local due = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1], 'LIMIT', 0, ARGV[2])
    if #due == 0 then return {} end
    redis.call('ZREM', KEYS[1], unpack(due))
    for i = 1, #due do
        redis.call('SADD', KEYS[2], due[i])
    end
    return due
"""
```

### 5. 状态机

```
pending ──worker 取──→ running ──成功──→ succeeded
   │                     │
   │                     ├──失败<max──→ pending（重新 push ZSet）
   │                     │
   │                     └──失败≥max──→ dead（dead letter）
   │
   ├──手动 trigger──→ pending（next_retry_at=NOW()）
   │
   └──手动 ignore──→ ignored
```

### 6. 退避策略

默认 exponential，±25% jitter 防雪崩：

```python
# backoff.py
delay_ms = base_ms * 2^(attempt_no - 1) * uniform(0.75, 1.25)
delay_ms = min(delay_ms, cap_ms=60_000)
```

参数（在 retry_task 表中按行存）：
- `backoff_policy`：`exponential` / `fixed` / `linear`
- `backoff_base_ms`：默认 1000ms
- `max_attempts`：默认 3

### 7. 路由声明顺序

FastAPI 静态段必须在 `{param}` 之前，否则 `/health` 会被 `/v1/retry/{retry_task_id}`
吞（int 验证 422）。`/v1/retry/health`、`/failed`、`/stats` 都在 `/{retry_task_id}` 之前。

## 接口

| 方法 | 路径 | 鉴权 | 说明 |
|------|------|------|------|
| GET  | `/v1/retry/failed` | 同租户 | 失败列表（since/until/api_id/app_id/status/limit/offset） |
| GET  | `/v1/retry/stats` | 同租户 | 重试统计（各状态计数 + 成功率 + top error） |
| GET  | `/v1/retry/{retry_task_id}` | 同租户 | 详情（含 attempts 历史） |
| POST | `/v1/retry/{retry_task_id}/trigger` | 同租户 | 手动触发重试（dead/ignored → pending） |
| POST | `/v1/retry/{retry_task_id}/ignore` | 同租户 | 标记忽略（不再自动重试） |
| GET  | `/v1/retry/health` | 无 | k8s probe |

## 本地开发

```bash
make install
make dev-up              # 起 PG/Redis/Kafka/CH/MinIO
make run-retry           # uvicorn retry_svc.main:app --port 8009
```

手动测一下：

```bash
# 模拟 executor 推一条失败到 Kafka
# （需要 Kafka client；或直接 POST admin API 触发）

# 失败列表
curl -s 'localhost:8009/v1/retry/failed' \
  -H 'X-API-Key: ak_test' | jq

# 详情
curl -s localhost:8009/v1/retry/1 \
  -H 'X-API-Key: ak_test' | jq

# 手动触发
curl -s -XPOST localhost:8009/v1/retry/1/trigger \
  -H 'X-API-Key: ak_test' | jq

# 统计
curl -s localhost:8009/v1/retry/stats \
  -H 'X-API-Key: ak_test' | jq
```

## 测试

```bash
cd services/services/retry
pytest tests/ -v
# 37 tests, all pass
```

覆盖：
- `test_backoff.py`（8）—— exponential / fixed / linear / cap / str policy / retry_count 边界
- `test_delay_queue.py`（8）—— schedule / pop_due / max_count / empty / complete / is_processing / list_tenants / get_due_count
- `test_consumer.py`（4）—— payload 解析 / 缺 tenant 跳过 / 非法 tenant / create 失败容错
- `test_worker.py`（7）—— 成功 / 失败 reschedule / 失败 dead letter / 跳过非 pending / executor timeout / tick 流转 / 无租户
- `test_routes.py`（10）—— failed / stats / detail 404 / trigger / ignore / health（含路由顺序验证）

mock 策略：
- Kafka：fake `_FakeKafkaMsg` + spy `process_task`
- Redis：`_FakeRedis` 替换 `raw_client()` + `_client`
- PG：spy `repo.*` 方法
- HTTP：`_FakeExecutorClient` 替换 `worker._client`

## 性能预算（prod）

- 3 副本起步（Kafka 16 partitions，最多扩到 10 副本）
- 单副本 1 CPU / 1Gi（消费 + 轮询 + 调 executor，CPU 偶发并发）
- HPA based on Kafka consumer group lag
- poll interval 1s（敏感度足够，CPU 开销小）
- batch_size 10 / tenant / tick

## 关联

- 上游：executor 推 `task-failures`；admin UI（失败任务后台）
- 依赖：PG (retry_task / retry_attempt)、Redis (ZSet 延迟队列)、Kafka (task-failures)
- 下游：executor HTTP 接口（重试触发）
