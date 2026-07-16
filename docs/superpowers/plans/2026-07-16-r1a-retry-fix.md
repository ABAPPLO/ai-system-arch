# R1a 救 retry 断链 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 真正接通 retry 链路——executor 失败时产 `task-failures`（R0b `TaskFailure` 契约），retry 自动消费建 retry_task、手动 trigger 能复活、at-least-once 重投不建重复行。

**Architecture:** executor 在 mark_failed 后 fire-and-forget 发 `TaskFailure`（显式 trace_id、int 字段用默认）；retry `trigger` 在 requeue 后调 `delay_queue.schedule`（按真实签名）；retry_task 加 partial unique(`task_instance_id` WHERE active) + `create_retry_task` ON CONFLICT DO NOTHING，consumer 对去重结果不重复入队。

**Tech Stack:** Python 3.11，asyncpg，aiokafka，Redis，PostgreSQL 15+，pytest。

## Spec refinements（读码时发现，已修正）

1. **`retry_task` 无 `task_id` 列**——实际是 `task_instance_id`（consumer 已设 `task_instance_id=failure.task_id`）。幂等键列 = `task_instance_id`。
2. **`status` CHECK 只允许 `('pending','running','succeeded','dead','ignored')`**——无 `'delayed'`（延迟在 Redis ZSet，PG status 停在 `pending`）。partial unique 的 active 集合 = `('pending','running')`。
3. **`delay_queue.schedule` 真实签名**：`schedule(*, tenant_id, retry_task_id, next_attempt_at_ts: float)`（keyword-only，unix 时间戳秒），**不是** `schedule(id, delay_ms)`。trigger 重入要用这个签名。

## Global Constraints

- 用 R0b 的 `TaskFailure` 契约（`from apihub_core.events import TaskFailure`）；**显式设 `trace_id=msg.trace_id or msg.task_id`**（R0b 硬约束，无回落）；`backoff_base_ms`/`max_attempts` 用 `TaskFailure` 默认（1000/3，int）。
- executor 只对 **failed/timeout** 发 `TaskFailure`（succeeded 不发）；fire-and-forget（包 `_suppress_kafka_err`）。
- 测试用 `.venv`（Python 3.11）；PG 集成测试用 `TEST_PG_DSN="postgresql://apihub_app:apihub_app_dev_pwd@localhost:15433/apihub"`。
- 一次 squash-PR；commit 按 task 本地。

---

## Task 1: executor 失败产 `TaskFailure`

**Files:**
- Modify: `services/services/executor/src/executor/processor.py`（import + 失败分支 emit）
- Test: `services/services/executor/tests/test_processor.py`（加断言）

**Interfaces:**
- Consumes: `apihub_core.events.TaskFailure`、`kafka.emit_event`、`TaskRequest`/`TaskResult`。
- Produces: executor 对 failed/timeout 任务产 `task-failures`（retry 消费）。

- [ ] **Step 1: Write the failing test**

在 `services/services/executor/tests/test_processor.py` 加（先 grep 现有 test_processor 怎么 mock `kafka.emit_event` / 构造失败 result，照搬其 fixture 与 process_task 调用方式）：

```python
async def test_failed_task_emits_task_failure(self, ...):  # ... = 现有 fixtures
    """failed 任务必须产 TaskFailure（带显式 trace_id），succeeded 不产。"""
    from apihub_core.events import TaskFailure
    # 用现有方式构造指向死后端的 TaskRequest，跑 process_task；mock kafka.emit_event 收集参数
    ...
    failures = [a for a in captured if isinstance(a, TaskFailure)]
    assert failures, "failed 任务应产 TaskFailure"
    assert failures[0].trace_id  # 显式非空
    assert failures[0].backend_url  # 回填给 retry 用
    # 另起一个 succeeded 用例（或同测内）断言 succeeded 不产 TaskFailure
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/services/executor && /home/applo/project/ai-system-arch/.venv/bin/python -m pytest tests/test_processor.py::test_failed_task_emits_task_failure -q`
Expected: FAIL — executor 现在不发 TaskFailure（`failures` 为空）。

- [ ] **Step 3: Write minimal implementation**

`processor.py` 顶部 import（现 `from apihub_core.events import TaskRequest, TaskStatus` → 加 `TaskFailure`）：

```python
from apihub_core.events import TaskFailure, TaskRequest, TaskStatus
```

在现有 `async with _suppress_kafka_err():` 块里（TaskStatus emit 之后，仍在块内）追加：

```python
        if result.status != "succeeded":
            await kafka.emit_event(
                TaskFailure(
                    task_id=msg.task_id,
                    tenant_id=tenant_id,
                    app_id=msg.app_id or "",
                    api_id=msg.api_id,
                    api_version_id=msg.api_version_id,
                    backend_url=msg.backend_url,
                    trace_id=msg.trace_id or msg.task_id,
                    request_id=msg.request_id or "",
                    payload=msg.payload,
                    error_code=result.error_code or "unknown",
                    error_msg=(result.error_msg or "")[:5000],
                    timeout_seconds=msg.timeout_seconds,
                )
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/services/executor && /home/applo/project/ai-system-arch/.venv/bin/python -m pytest tests/test_processor.py -q`
Expected: PASS（含新测试 + 现有不回归）。

- [ ] **Step 5: ruff + commit**

```bash
cd services/services/executor && /home/applo/project/ai-system-arch/.venv/bin/ruff check src/executor/processor.py tests/test_processor.py
git add services/services/executor/src/executor/processor.py services/services/executor/tests/test_processor.py
git commit -m "feat(executor): emit TaskFailure on task failure (R1a §2.1)"
```

---

## Task 2: retry `trigger` 重入延迟队列

**Files:**
- Modify: `services/services/retry/src/retry_svc/routes.py`（`trigger_retry` handler）
- Test: `services/services/retry/tests/test_routes.py`（加断言）

**Interfaces:**
- Consumes: `delay_queue.schedule(*, tenant_id, retry_task_id, next_attempt_at_ts)`、`repo.requeue_for_retry -> (ok, tenant)`。
- Produces: 手动 trigger 后任务进 Redis 延迟队列，worker 能取到。

- [ ] **Step 1: Write the failing test**

在 `tests/test_routes.py` 加（grep 现有 trigger 测试的 client/fixture 风格照搬）：

```python
async def test_trigger_re_enqueues_to_delay_queue(self, client, monkeypatch):
    """trigger 成功后必须 delay_queue.schedule，否则 worker 取不到。"""
    called = {}
    async def _fake_schedule(*, tenant_id, retry_task_id, next_attempt_at_ts):
        called["args"] = (tenant_id, retry_task_id)
    monkeypatch.setattr("retry_svc.routes.delay_queue.schedule", _fake_schedule)

    async def _fake_requeue(retry_task_id):
        return (True, "tenant_a")
    monkeypatch.setattr("retry_svc.routes.repo.requeue_for_retry", _fake_requeue)

    resp = await client.post("/v1/retry/42/trigger")
    assert resp.status_code == 200
    assert called.get("args") == ("tenant_a", 42), "trigger 必须 re-enqueue"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/services/retry && /home/applo/project/ai-system-arch/.venv/bin/python -m pytest tests/test_routes.py::test_trigger_re_enqueues_to_delay_queue -q`
Expected: FAIL — 现有 trigger 不调 schedule，`called` 为空。

- [ ] **Step 3: Write minimal implementation**

`routes.py` 顶部 import 加 `import time` + `from retry_svc import delay_queue`（若未导入）。`trigger_retry` handler（现 `routes.py:82-`）在 `ok, _tenant = await repo.requeue_for_retry(retry_task_id)` 成功分支内补：

```python
        ok, _tenant = await repo.requeue_for_retry(retry_task_id)
        if ok:
            await delay_queue.schedule(
                tenant_id=_tenant,
                retry_task_id=retry_task_id,
                next_attempt_at_ts=time.time(),
            )
            return {"requeued": True, "retry_task_id": retry_task_id}
        raise ApiError(...)  # 现有 not-found 分支保持
```

（`_tenant` 是 requeue_for_retry 返回的 tenant；`time.time()` = 立即到期。）

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/services/retry && /home/applo/project/ai-system-arch/.venv/bin/python -m pytest tests/test_routes.py -q`
Expected: PASS。

- [ ] **Step 5: ruff + commit**

```bash
cd services/services/retry && /home/applo/project/ai-system-arch/.venv/bin/ruff check src/retry_svc/routes.py tests/test_routes.py
git add services/services/retry/src/retry_svc/routes.py services/services/retry/tests/test_routes.py
git commit -m "fix(retry): trigger re-enqueues to delay queue (R1a §2.2)"
```

---

## Task 3: retry_task 幂等（partial unique + ON CONFLICT）

**Files:**
- Create: `scripts/init-db/10-r1a-retry-idempotency.sql`
- Modify: `services/services/retry/src/retry_svc/repository.py`（`create_retry_task` INSERT + 返回 0 表去重）
- Modify: `services/services/retry/src/retry_svc/consumer.py`（去重时不重复入队）
- Test: `services/services/retry/tests/test_repository.py`（PG 集成，去重断言）

**Interfaces:**
- Consumes: PG partial unique index。
- Produces: `create_retry_task(...) -> int`（0=去重跳过，>0=新 id）；consumer 据 0 跳过 schedule。

- [ ] **Step 1: Write the migration**

Create `scripts/init-db/10-r1a-retry-idempotency.sql`:

```sql
-- R1a §2.7: retry_task 幂等 —— 同一 task_instance_id 同时只允许一个活跃 retry_task。
-- 上次重试已 dead/succeeded/ignored 后再次失败可建新行（partial unique 只约束活跃集）。
DROP INDEX IF EXISTS idx_retry_task_active_dedup;
CREATE UNIQUE INDEX IF NOT EXISTS idx_retry_task_active_dedup
  ON retry_task(task_instance_id) WHERE status IN ('pending', 'running');
```

- [ ] **Step 2: Write the failing test**

在 `tests/test_repository.py` 加（PG 集成，需 dev 栈；用 `TEST_PG_DSN` + 现有 repo 测试的 pool/conn fixture）：

```python
async def test_create_retry_task_dedups_active(self, ...):
    """同 task_instance_id 已有活跃 retry_task 时，再次插入返回 0（去重），不报错。"""
    from datetime import UTC, datetime, timedelta
    nxt = datetime.now(UTC) + timedelta(seconds=1)
    rid1 = await repo.create_retry_task(
        tenant_id="t1", trace_id="trc_1", api_id="api1", app_id="a1",
        task_instance_id="task_dup", original_request={},
        error_code="x", error_msg="y", max_attempts=3,
        backoff_policy=BackoffPolicy.EXPONENTIAL, backoff_base_ms=1000,
        next_retry_at=nxt, env="dev",
    )
    assert rid1 > 0
    rid2 = await repo.create_retry_task(  # 同 task_instance_id="task_dup"
        tenant_id="t1", trace_id="trc_1", api_id="api1", app_id="a1",
        task_instance_id="task_dup", original_request={},
        error_code="x", error_msg="y", max_attempts=3,
        backoff_policy=BackoffPolicy.EXPONENTIAL, backoff_base_ms=1000,
        next_retry_at=nxt, env="dev",
    )
    assert rid2 == 0  # 去重
```

（fixture 以 test_repository 现有为淮；测试后清理该 task_dup 行。）

- [ ] **Step 3: Run test to verify it fails**

Run: `cd services/services/retry && TEST_PG_DSN="postgresql://apihub_app:apihub_app_dev_pwd@localhost:15433/apihub" /home/applo/project/ai-system-arch/.venv/bin/python -m pytest tests/test_repository.py::test_create_retry_task_dedups_active -q`
Expected: FAIL — 无 unique index，第二次插入成功返回 id>0（rid2 != 0）。

- [ ] **Step 4: Apply migration to dev DB + implement ON CONFLICT**

对 dev PG 应用迁移：

```bash
docker exec -i apihub-pg psql -U apihub -d apihub < scripts/init-db/10-r1a-retry-idempotency.sql
```

`repository.create_retry_task` 的 INSERT 加 `ON CONFLICT DO NOTHING` 并处理空返回：

```python
            INSERT INTO retry_task (...) VALUES (...)
            ON CONFLICT DO NOTHING
            RETURNING id
            """, ...)
        return row["id"] if row else 0   # 0 = 去重跳过
```

把 docstring 里"假设 Kafka 不重投"那段改成"靠 partial unique idx_retry_task_active_dedup 去重，返回 0 表去重"。

`consumer.py` 在 `retry_task_id = await repo.create_retry_task(...)` 之后，把 schedule 包进 `if retry_task_id:`（去重则跳过）：

```python
        retry_task_id = await repo.create_retry_task(...)
        if not retry_task_id:
            log.info("retry_task_deduped", task_id=failure.task_id)
            return  # 已有活跃 retry_task，at-least-once 重投去重
        await delay_queue.schedule(...)
```

- [ ] **Step 5: Run test to verify it passes**

Run: 同 Step 3 命令。
Expected: PASS（rid2 == 0）。再跑 retry 全量 `tests/ -q` 确认不回归（2 个既存 test_worker 失败仍在，记 follow-up）。

- [ ] **Step 6: ruff + commit**

```bash
cd services/services/retry && /home/applo/project/ai-system-arch/.venv/bin/ruff check src/retry_svc/repository.py src/retry_svc/consumer.py tests/test_repository.py
git add scripts/init-db/10-r1a-retry-idempotency.sql services/services/retry/src/retry_svc/repository.py services/services/retry/src/retry_svc/consumer.py services/services/retry/tests/test_repository.py
git commit -m "feat(retry): retry_task idempotency via partial unique (R1a §2.7)"
```

---

## Task 4: 回归 + e2e 验证 runbook + PR

**Files:**
- Create: `scripts/smoke/r1a-retry-chain.py`（e2e runbook：从 task-requests 真实入口验证自动重试链，禁手动注入 task-failures）

- [ ] **Step 1: 全量回归**

```bash
for svc in libs/apihub-core services/dispatcher services/executor services/retry; do
  (cd services/$svc && TEST_PG_DSN="postgresql://apihub_app:apihub_app_dev_pwd@localhost:15433/apihub" /home/applo/project/ai-system-arch/.venv/bin/python -m pytest -q 2>&1 | tail -1)
done
```
Expected: apihub-core/dispatcher/executor 绿；retry 仅 2 个既存 test_worker 失败。

- [ ] **Step 2: 写 e2e runbook 脚本**

Create `scripts/smoke/r1a-retry-chain.py`：从 `task-requests` topic 投一条 backend_url=`http://127.0.0.1:9/x`（连接拒绝）的任务 → executor 消费失败 → **自动**产 task-failures → retry 消费建 retry_task → 轮询 PG `retry_task`/`retry_attempt` 直到 `status='dead'`、`retry_count==max_attempts`。脚本顶部注释强调"**禁止**手动注入 task-failures——本脚本只投 task-requests，证明 executor 的 task-failures 生产真闭合（审计 §6）"。前置：executor + retry 服务在跑（`make run-executor` / `make run-retry`）+ Kafka/Redis/PG 起。参考现有 `scripts/smoke/k8s-links.py` 的 aiokafka produce + 轮询 PG 模式写完整可运行脚本。

- [ ] **Step 3: （可选）跑 e2e runbook**

若本机 executor/retry 已起（或现起），跑 `python scripts/smoke/r1a-retry-chain.py`，确认 retry_task 自动出现并到 dead。若起服务太重，跳过实跑、在 PR 描述注明"runbook 已就绪，待有服务实例时人工验证"。

- [ ] **Step 4: commit + PR**

```bash
git add scripts/smoke/r1a-retry-chain.py
git commit -m "test: R1a retry-chain e2e runbook (no manual task-failures injection)"
```

PR description 必含：
- R1a 关闭审计 §2.1（executor 产 task-failures）/§2.2（trigger 重入）/§2.7（幂等）。
- 用 R0b TaskFailure 契约；显式 trace_id、int 默认字段。
- 核心验证：e2e runbook 从 task-requests 真实入口验证自动链（禁手动注入 task-failures）。
- Spec refinement：幂等键列 = `task_instance_id`（非 task_id）；active statuses `('pending','running')`（无 delayed）；`delay_queue.schedule` 真实签名。
- Follow-up：retry 2 个既存 test_worker 失败；"可重试 error_code 白名单"未做（所有 failed 进 retry）。

---

## Self-Review

1. **Spec coverage**：§2.1→Task 1；§2.2→Task 2；§2.7→Task 3；§3 e2e→Task 4；R0b 硬约束（trace_id/int）→Task 1 代码。✓
2. **Placeholder scan**：Task 1/2 测试用"以现有 fixture 为准"——实现者需读现有 test_processor/test_routes 复用风格（明确指令，非占位）。Task 4 runbook 参考现有 k8s-links.py 写完整脚本（明确参考）。无 TBD。✓
3. **Type consistency**：`TaskFailure` 字段（Task 1）与 R0b events.py 一致；`delay_queue.schedule(*, tenant_id, retry_task_id, next_attempt_at_ts)`（Task 2）与 delay_queue.py:25 一致；`create_retry_task -> int`（0=去重，Task 3）与 consumer 判断一致；幂等列 `task_instance_id`（Task 3）与 consumer `task_instance_id=failure.task_id` 一致。✓
4. **风险**：partial unique 的 ON CONFLICT 推断——PG15+ 支持 `ON CONFLICT DO NOTHING`（无列）按任意 unique index 推断；Task 3 Step 3/5 实测确认。e2e runbook 需活服务——Task 4 Step 3 允许跳过实跑、PR 注明。✓
