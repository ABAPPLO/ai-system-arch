# R1a 救 retry 断链 设计

日期：2026-07-16
状态：Draft（待 review）
依据：[phase4-audit-findings.md](../../phase4-audit-findings.md) §2.1/2.2/2.7（retry 自动/手动链路断裂 + 无幂等）；[r0b-...-design.md](2026-07-15-r0b-kafka-event-contract-design.md) 的 `TaskFailure` 契约（已合入 main, commit b73bad5）。
范围决策：**幂等键 = partial unique on active task_id**（用户 2026-07-16 拍板，方案 A）。

---

## 1. 背景与目标

审计最严重发现（§2.1）：**全仓无人向 Kafka `task-failures` 投递** → retry 的自动重试/死信机制在生产永不触发。配合 §2.2（手动 `trigger` 不重入 Redis 延迟队列，静默失效）+ §2.7（retry_task 无幂等约束）。phase2 "#99 验证通过"靠 smoke 脚本手动注入掩盖了生产者缺失。

R0b 已建立 `TaskFailure` typed 契约（main, b73bad5），并给 R1a 留了两条硬约束：**生产时必须显式设 `trace_id`**（新 dataclass 无 `trace_id or task_id` 回落）、**`backoff_base_ms`/`max_attempts` 必须是 int**。

R1a 目标：**真正接通 retry 链路**——executor 在任务失败时产 `task-failures`（走 R0b 契约），retry 自动消费建 retry_task → 延迟队列 → 重投 executor → 死信；手动 trigger 也能复活；at-least-once 重投不建重复行。

**成功标准（核心）**：从 **HTTP 真实入口**制造一次 backend 失败（dispatcher → executor → 失败），观察 retry_task **自动**创建并按指数退避重试到 `dead`——**禁止 smoke 脚本手动注入 task-failures**（审计 §6 方法论教训）。手动 trigger 也能把 dead 任务复活到 worker 轮询队列。

## 2. 三处改动

### 2.1 executor 产 `task-failures`（`executor/processor.py`）
在任务失败路径（`mark_failed` 分支，含 timeout/RequestError 映射到的 failed），fire-and-forget 发一条 `TaskFailure`：

```python
await kafka.emit_event(
    TaskFailure(
        task_id=msg.task_id,
        tenant_id=tenant_id,
        app_id=msg.app_id or "",
        api_id=msg.api_id,
        api_version_id=msg.api_version_id,
        backend_url=msg.backend_url,
        trace_id=msg.trace_id or msg.task_id,   # R0b 硬约束：显式设，无回落
        request_id=msg.request_id or "",
        payload=msg.payload,
        error_code=result.error_code or "unknown",
        error_msg=result.error_msg or "",
        timeout_seconds=msg.timeout_seconds,
        # backoff_base_ms/max_attempts 用 TaskFailure 默认（1000/3）；
        # 若将来要按租户/API 配置，由 retry 侧查 quota/tenant 规则覆盖
    )
)
```
- 与现有 `TaskStatus` emit（processor.py:89）并列，同样包在 `_suppress_kafka_err()` 里（投递失败不影响业务）。
- **只对 failed/timeout 发**（succeeded 不发）。
- 字段 `msg.*` 来自 `TaskRequest`（R0b 契约）；`result.*` 来自 processor 的 `TaskResult`。

### 2.2 retry `trigger` 重入延迟队列（`retry/repository.py:requeue_for_retry`）
现状：`requeue_for_retry` 只 SQL UPDATE（status=pending + next_retry_at=NOW()），不调 `delay_queue.schedule` → worker（只轮询 Redis ZSet）取不到。改为 UPDATE 成功后补：

```python
ok, tenant = await requeue_for_retry(retry_task_id)   # 现有
if ok:
    await delay_queue.schedule(retry_task_id, delay_ms=0)   # 新增：立即入队
```
（在 route handler `trigger_retry` 或 `requeue_for_retry` 内部择一；优先在 handler 内，保持 repository 纯 DB。）`delay_queue.schedule` 签名以现有 `retry/delay_queue.py` 为准。

### 2.3 retry_task 幂等（partial unique + ON CONFLICT）
迁移 SQL（`scripts/init-db/`，新增一个 `10-r1a-retry-idempotency.sql` 或追加 phase2）：

```sql
DROP INDEX IF EXISTS idx_retry_task_active_dedup;
CREATE UNIQUE INDEX idx_retry_task_active_dedup
  ON retry_task(task_id) WHERE status IN ('pending', 'delayed', 'running');
```
语义：同一 `task_id` 同时只允许一个**活跃** retry_task；上次重试已 `dead`/`succeeded` 后再次失败可建新行（方案 A）。

`retry/repository.create_retry_task` 的 INSERT 加 `ON CONFLICT (task_id) WHERE status IN ('pending','delayed','running') DO NOTHING`，返回是否真插入（False=重复，幂等跳过，不报错）。

> 注：PG `ON CONFLICT` 需指向具体唯一索引。partial unique index 的 `ON CONFLICT` 用推断（`ON CONFLICT DO NOTHING` 不带列时按任意冲突）或显式 `(task_id) WHERE ...`——实现期以 PG 版本实测为准（phase2 用 PG 15+，支持 partial index 冲突推断）。

## 3. 测试

- **单测**：executor 失败路径产 TaskFailure（mock emit_event 捕获 TaskFailure 实例，断言 trace_id 显式、字段齐全、只在 failed 发）；requeue 后 delay_queue.schedule 被调；create_retry_task ON CONFLICT 重复插入返回 False 不报错。
- **端到端（核心，Task 4）**：起 dev 栈（PG@15433 + Redis + Kafka 已在跑），用真实 HTTP 入口（dispatcher `/v1/dispatch/...` 指向一个死后端 `http://127.0.0.1:9/x`）触发异步任务 → executor 失败 → 产 task-failures → retry 自动建 retry_task → 指数退避重试到 `dead`。断言：retry_task 自动出现（非手动注入）、retry_attempt 有 N 条、最终 `dead`。**这条测试证明 §2.1 真闭合**。
- 回归：apihub-core / dispatcher / executor / retry 现有测试不回归（retry 的 2 个既存 test_worker 失败仍在，记 follow-up）。

## 4. 风险

- **端到端依赖活 Kafka**：dev Kafka（apihub-kafka）在跑；若 topic `task-failures` 未建，需 `scripts/init-kafka/create-topics.sh` 含它（phase2 已建，确认）。
- **ON CONFLICT 语义**：partial unique 的冲突推断在 PG 各版本行为一致（15+），但实现期实测 INSERT 重复行确认真被跳过。
- **executor 对所有失败发 task-failures**：可能放大 retry 量。可接受（retry 有 max_attempts→dead 兜底）；未来可加"是否可重试"判断（error_code 白名单），不在 R1a。
- **trace_id 回落**：`msg.trace_id or msg.task_id`——若 R0b 的 TaskRequest.trace_id 为空（dispatcher 未传），回落 task_id 保证非空（满足 retry_task.trace_id NOT NULL）。

## 5. 不做（R1a 边界）

- 不做"可重试 error_code 白名单"（所有 failed 都进 retry，max_attempts 兜底）。
- 不动 retry 的 backoff 策略 / dead-letter 后续处理。
- 不补 retry 的 stale-running 恢复（审计 §4，P2）。
- 不动 executor 的 webhook 回调（审计 §4）。

## 6. 下一步

R1a spec 经用户复核 → writing-plans 出 TDD 计划 → subagent 执行 → 一个 squash-PR。
