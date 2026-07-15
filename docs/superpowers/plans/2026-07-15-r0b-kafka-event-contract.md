# R0b Kafka 事件契约 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `apihub_core` 建立 typed Kafka 事件契约（4 个 frozen dataclass + `emit_event`/`parse_event` helper），并把形状吻合的现有生产/消费迁过去——为 R1a（救 retry 断链）铺路。

**Architecture:** 新建 `apihub_core/events.py`（4 个 `@dataclass(frozen=True, slots=True)` + `TOPIC: ClassVar` + `from_dict` 容忍多余字段）；`kafka.py` 加 `emit_event(event)`（`asdict` → 复用现有 `emit`，保留 tenant/traceparent/span 注入）和 `parse_event(topic, payload)`（按 topic 路由到 dataclass）。迁移形状吻合的点；R0b 自身零行为变更。

**Tech Stack:** Python 3.11，dataclasses，aiokafka，pytest（`asyncio_mode=auto`）。

## Spec refinement（读码时发现）

1. **quota 的 `api-call-events` emit 形状不同**（`{event_type:"quota_check", allowed, tier_blocked, cost, rule_source}`），不是 CallEvent。R0b **不迁 quota**——它是独立的 QuotaDecisionEvent，留 follow-up（定义独立 topic `quota-decisions` 或带 discriminator 的契约）。
2. **notification 的 api-call-events 消费**只透传 webhook，字段使用面小；R0b 不强制迁，留 follow-up。
3. 因此 R0b 实际迁移：生产 = dispatcher(task-requests, api-call-events→CallEvent) + executor(task-status)；消费 = executor(task-requests) + retry(task-failures)。这些是形状吻合、且 retry 断链相关的核心路径。

## Global Constraints

- **零行为变更**：同样的事件在流，只是 typed；现有服务测试必须不回归。
- `CallEvent.is_success` / `ai_streaming` 是 **int(0/1) 非 bool**（对齐 ClickHouse UInt8）——dataclass 字段用 `int`。
- `parse_event` **容忍多余字段**（只取已知字段），未知字段忽略（可记 debug log）；必填字段缺失抛 `TypeError`。
- 旧 `kafka.emit(topic, dict)` **保留**（quota 等未迁的继续用）。
- `task-failures` 仍无生产者（R1a 才补）。
- 一次 squash-PR；commit 按 task 本地。

---

## Task 1: `apihub_core/events.py` — 4 个 typed 事件 + round-trip 测试

**Files:**
- Create: `services/libs/apihub-core/src/apihub_core/events.py`
- Test: `services/libs/apihub-core/tests/test_events.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `TaskRequest`, `TaskStatus`, `TaskFailure`, `CallEvent`（均带 `TOPIC: ClassVar[str]` + classmethod `from_dict(payload)`）；模块级 `_from_dict(cls, payload)` helper。

- [ ] **Step 1: Write the failing test**

Create `services/libs/apihub-core/tests/test_events.py`:

```python
"""R0b: Kafka 事件契约 round-trip + 容忍多余字段。纯单测，无 PG/Redis/Kafka。"""

from dataclasses import asdict

import pytest

from apihub_core import kafka  # noqa: F401  (确保可 import；Task 2 才加 parse_event)
from apihub_core.events import CallEvent, TaskFailure, TaskRequest, TaskStatus


def _call_event(**over):
    base = dict(
        ts="2026-07-15 10:00:00.000", tenant_id="t1", tenant_type="internal", app_id="a1",
        api_id="api1", api_version_id="v1", trace_id="trc_1", request_id="req_1",
        method="GET", path="/x", status_code=200, is_success=1, latency_ms=5,
        request_size=10, response_size=20,
    )
    base.update(over)
    return CallEvent(**base)


@pytest.mark.parametrize("evt", [
    TaskRequest(task_id="tk_1", api_id="api1", api_version_id="v1", backend_url="http://b"),
    TaskStatus(task_id="tk_1", tenant_id="t1", app_id="a1", api_id="api1", status="succeeded"),
    TaskFailure(task_id="tk_1", tenant_id="t1", api_id="api1", backend_url="http://b"),
    _call_event(),
])
def test_topic_constant(evt):
    assert evt.TOPIC in {"task-requests", "task-status", "task-failures", "api-call-events"}


def test_call_event_int_fields_not_bool():
    e = _call_event(is_success=0, ai_streaming=1)
    assert isinstance(e.is_success, int) and isinstance(e.ai_streaming, int)


def test_from_dict_roundtrip():
    e = _call_event(error_code="backend_timeout")
    assert CallEvent.from_dict(asdict(e)) == e


def test_from_dict_ignores_extra_fields():
    e = TaskStatus(task_id="tk", tenant_id="t", app_id="a", api_id="api", status="failed")
    payload = {**asdict(e), "unknown_future_col": "x", "another": 123}
    assert TaskStatus.from_dict(payload) == e  # 多余字段被忽略，不抛


def test_from_dict_missing_required_raises():
    with pytest.raises(TypeError):
        TaskRequest.from_dict({"api_id": "api1"})  # 缺 task_id/backend_url 等


def test_frozen():
    e = TaskStatus(task_id="tk", tenant_id="t", app_id="a", api_id="api", status="failed")
    with pytest.raises(Exception):
        e.status = "succeeded"  # frozen
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/libs/apihub-core && ../../.venv/bin/python -m pytest tests/test_events.py -q` (用仓库根 `.venv` 的 Python 3.11).
Expected: FAIL — `ModuleNotFoundError: apihub_core.events`.

- [ ] **Step 3: Write minimal implementation**

Create `services/libs/apihub-core/src/apihub_core/events.py`:

```python
"""Kafka 事件契约 —— 4 个 typed 事件的单一事实源。

生产用 kafka.emit_event(event)，消费用 kafka.parse_event(topic, payload)。
字段逐字来自原各服务手搓 payload（见 docs/superpowers/specs/2026-07-15-r0b-kafka-event-contract-design.md）。
新增字段一律加默认值，旧消费者 parse_event 容忍多余字段、向前兼容。
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import ClassVar


def _from_dict(cls, payload: dict):
    """容忍多余字段：只取 dataclass 已知字段；缺必填字段由 dataclass 抛 TypeError。"""
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in (payload or {}).items() if k in names})


@dataclass(frozen=True, slots=True)
class TaskRequest:
    """dispatcher → executor 的任务请求（原 executor.models.TaskMessage 的字段）。"""

    TOPIC: ClassVar[str] = "task-requests"
    task_id: str
    api_id: str
    api_version_id: str
    backend_url: str
    payload: str = ""
    timeout_seconds: float = 30.0
    callback_url: str | None = None
    # 以下通常从 Kafka header 取（kafka.emit 自动注入 tenant_id/app_id）
    tenant_id: str | None = None
    app_id: str | None = None
    request_id: str | None = None
    trace_id: str | None = None

    @classmethod
    def from_dict(cls, payload):
        return _from_dict(cls, payload)


@dataclass(frozen=True, slots=True)
class TaskStatus:
    """executor → observability/notifier 的任务状态变更。"""

    TOPIC: ClassVar[str] = "task-status"
    task_id: str
    tenant_id: str
    app_id: str
    api_id: str
    status: str  # succeeded / failed / timeout
    error_code: str = ""
    duration_ms: int = 0
    request_id: str = ""

    @classmethod
    def from_dict(cls, payload):
        return _from_dict(cls, payload)


@dataclass(frozen=True, slots=True)
class TaskFailure:
    """executor → retry 的失败任务（R0b 只定义契约；executor 生产留 R1a）。

    字段对齐 retry_svc.models.FailureMessage + retry/consumer.py:_handle 的期望。
    """

    TOPIC: ClassVar[str] = "task-failures"
    task_id: str
    tenant_id: str
    api_id: str
    backend_url: str
    app_id: str = ""
    api_version_id: str | None = None
    trace_id: str = ""
    request_id: str = ""
    payload: str = ""
    error_code: str = "unknown"
    error_msg: str = ""
    timeout_seconds: float = 30.0
    max_attempts: int = 3
    backoff_base_ms: int = 1000

    @classmethod
    def from_dict(cls, payload):
        return _from_dict(cls, payload)


@dataclass(frozen=True, slots=True)
class CallEvent:
    """dispatcher/quota → ClickHouse/notification 的调用事件。

    字段逐字来自 dispatcher.event.build_call_event，与 ClickHouse api_call_log schema 对齐。
    is_success / ai_streaming 是 int(0/1)（CH UInt8），不是 bool。
    """

    TOPIC: ClassVar[str] = "api-call-events"
    ts: str
    tenant_id: str
    tenant_type: str
    app_id: str
    api_id: str
    api_version_id: str
    trace_id: str
    request_id: str
    method: str
    path: str
    status_code: int
    is_success: int
    latency_ms: int
    request_size: int
    response_size: int
    error_code: str = ""
    error_msg: str = ""
    user_agent: str = ""
    client_ip: str = "0.0.0.0"
    backend_type: str = "http"
    backend_latency_ms: int = 0
    ai_model: str = ""
    ai_streaming: int = 0
    token_prompt: int = 0
    token_completion: int = 0
    token_total: int = 0

    @classmethod
    def from_dict(cls, payload):
        return _from_dict(cls, payload)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/libs/apihub-core && ../../.venv/bin/python -m pytest tests/test_events.py -q`
Expected: passed（含 parametrize 展开）。

- [ ] **Step 5: ruff + commit**

```bash
cd services/libs/apihub-core && ../../.venv/bin/ruff check src/apihub_core/events.py tests/test_events.py
git add services/libs/apihub-core/src/apihub_core/events.py services/libs/apihub-core/tests/test_events.py
git commit -m "feat(apihub-core): typed Kafka event contract (R0b)"
```

---

## Task 2: `kafka.emit_event` + `parse_event` helper

**Files:**
- Modify: `services/libs/apihub-core/src/apihub_core/kafka.py` (add `emit_event`, `parse_event`, `_TOPIC_TO_EVENT`)
- Test: `services/libs/apihub-core/tests/test_events.py` (append)

**Interfaces:**
- Consumes: Task 1 的 4 个 dataclass；现有 `emit()`。
- Produces: `kafka.emit_event(event) -> None`、`kafka.parse_event(topic: str, payload: dict) -> <Event>`。

- [ ] **Step 1: Write the failing test**

Append to `tests/test_events.py`:

```python
from apihub_core import kafka as core_kafka


def test_parse_event_routes_by_topic():
    payload = {"task_id": "tk", "tenant_id": "t", "app_id": "a", "api_id": "api",
               "status": "failed", "future_col": "ignored"}
    evt = core_kafka.parse_event("task-status", payload)
    assert isinstance(evt, TaskStatus)
    assert evt.status == "failed"


def test_parse_event_unknown_topic_raises():
    import pytest
    with pytest.raises(ValueError):
        core_kafka.parse_event("nope-topic", {"a": 1})


def test_emit_event_requires_topic():
    # emit_event 取 event.TOPIC；无 TOPIC 的对象应拒
    import asyncio
    import pytest
    class NoTopic:
        pass
    with pytest.raises(TypeError):
        asyncio.get_event_loop().run_until_complete(core_kafka.emit_event(NoTopic()))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/libs/apihub-core && ../../.venv/bin/python -m pytest tests/test_events.py -q`
Expected: FAIL — `parse_event` / `emit_event` not defined。

- [ ] **Step 3: Write minimal implementation**

Edit `services/libs/apihub-core/src/apihub_core/kafka.py`. 在文件顶部 import 区加：

```python
from dataclasses import asdict

from apihub_core.events import CallEvent, TaskFailure, TaskRequest, TaskStatus
```

在 `extract_trace_context` 之后（`consume_span` 之前）加：

```python
_TOPIC_TO_EVENT: dict[str, type] = {
    TaskRequest.TOPIC: TaskRequest,
    TaskStatus.TOPIC: TaskStatus,
    TaskFailure.TOPIC: TaskFailure,
    CallEvent.TOPIC: CallEvent,
}


def parse_event(topic: str, payload: dict):
    """按 topic 把 payload 解析成 typed 事件。容忍多余字段；未知 topic 抛 ValueError。"""
    cls = _TOPIC_TO_EVENT.get(topic)
    if cls is None:
        raise ValueError(f"未知 topic，无事件契约: {topic}")
    return cls.from_dict(payload)


async def emit_event(event) -> None:
    """投递 typed 事件：asdict → 复用 emit()（保留 tenant/traceparent/PRODUCER span 注入）。

    key 优先用 task_id（同任务消息进同分区），否则 None（emit 会回落到 tenant_id）。
    """
    topic = getattr(event, "TOPIC", None)
    if not topic:
        raise TypeError(f"{type(event).__name__} 未定义 TOPIC，不能 emit_event")
    payload = asdict(event)
    key = payload.get("task_id") or None
    await emit(topic, payload, key=key)
```

若 `apihub_core/__init__.py` re-export 了 kafka 符号（先 `grep "from .kafka import" src/apihub_core/__init__.py`），把 `emit_event, parse_event` 补进导出；否则跳过。

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/libs/apihub-core && ../../.venv/bin/python -m pytest tests/test_events.py -q`
Expected: all passed（Task 1 + Task 2）。

- [ ] **Step 5: 回归 apihub-core 全量 + ruff + commit**

```bash
cd services/libs/apihub-core && ../../.venv/bin/python -m pytest -q && ../../.venv/bin/ruff check src/apihub_core/kafka.py src/apihub_core/events.py
git add services/libs/apihub-core/src/apihub_core/kafka.py services/libs/apihub-core/src/apihub_core/__init__.py services/libs/apihub-core/tests/test_events.py
git commit -m "feat(apihub-core): emit_event/parse_event helpers (R0b)"
```

---

## Task 3: 迁移生产侧（dispatcher × 2 + executor × 1）

**Files:**
- Modify: `services/services/dispatcher/src/dispatcher/task_dispatcher.py:49`（task-requests emit）
- Modify: `services/services/dispatcher/src/dispatcher/event.py`（`build_call_event` 返回 CallEvent）+ `dispatcher/forwarder.py:286/311/349`（改用 emit_event）
- Modify: `services/services/executor/src/executor/processor.py:88`（task-status emit）
- Test: 各服务现有测试（无新测试，行为零变更）

**Interfaces:**
- Consumes: Task 1+2 的 `TaskRequest` / `CallEvent` / `TaskStatus` / `emit_event`。
- Produces: 生产者走契约。

- [ ] **Step 1: dispatcher task-requests**

在 `dispatcher/task_dispatcher.py`，把现有 `await kafka.emit("task-requests", {...}, key=task_id, extra_headers={...})` 整块替换为：

```python
    await kafka.emit_event(
        TaskRequest(
            task_id=task_id,
            api_id=snap.api_id,
            api_version_id=snap.id,
            backend_url=snap.backend_url,
            payload=body.decode("utf-8", errors="replace"),
            request_id=request_id,
        )
    )
```

并加 import：`from apihub_core.events import TaskRequest`（保留原 `from apihub_core import kafka`）。

- [ ] **Step 2: dispatcher api-call-events → CallEvent**

在 `dispatcher/event.py`，把 `build_call_event(...)` 的 `return {...}` 改为构造 `CallEvent`：保留现有字段组装成 dict `d`，末尾 `return CallEvent(**d)`（键名与 CallEvent 字段逐字对应，见 spec §2.4）。在 `dispatcher/forwarder.py`，把三处 `await kafka.emit("api-call-events", payload)`（行 286/311/349）改为 `await kafka.emit_event(payload)`（`payload = build_call_event(...)` 现在返回 CallEvent）。grep `build_call_event` 在 dispatcher/tests/ 下：若断言 `payload["method"]` 之类 dict 访问，改属性访问 `payload.method`。

- [ ] **Step 3: executor task-status**

在 `executor/processor.py:88`，把 `await kafka.emit("task-status", {...}, key=msg.task_id, extra_headers={"request_id": msg.request_id or ""})` 替换为：

```python
        await kafka.emit_event(
            TaskStatus(
                task_id=msg.task_id,
                tenant_id=tenant_id,
                app_id=msg.app_id or "",
                api_id=msg.api_id,
                status=result.status,
                error_code=result.error_code or "",
                duration_ms=result.duration_ms,
                request_id=msg.request_id or "",
            )
        )
```

加 import `from apihub_core.events import TaskStatus`。

- [ ] **Step 4: 跑 dispatcher + executor 现有测试确认零回归**

```bash
cd services/services/dispatcher && ../../.venv/bin/python -m pytest -q
cd ../executor && ../../.venv/bin/python -m pytest -q
```
Expected: 全绿（含把 build_call_event dict 断言改属性访问后的测试）。

- [ ] **Step 5: ruff + commit**

```bash
cd services/services/dispatcher && ../../.venv/bin/ruff check src/dispatcher/event.py src/dispatcher/forwarder.py src/dispatcher/task_dispatcher.py
cd ../executor && ../../.venv/bin/ruff check src/executor/processor.py
git add services/services/dispatcher services/services/executor
git commit -m "refactor: migrate producers to typed event contract (R0b)"
```

---

## Task 4: 迁移消费侧（executor task-requests + retry task-failures）

**Files:**
- Modify: `services/services/executor/src/executor/consumer.py`（task-requests parse）+ `executor/models.py`（TaskMessage）+ `processor.py`（类型注解）
- Modify: `services/services/retry/src/retry_svc/consumer.py`（task-failures parse）
- Test: executor + retry 现有测试（行为零变更）

**Interfaces:**
- Consumes: Task 1+2 的 `TaskRequest` / `TaskFailure` / `parse_event`。
- Produces: 消费者走契约。

- [ ] **Step 1: executor task-requests 消费**

`executor/consumer.py:_handle` 现用 `TaskMessage(...)` 直构。把整段 `task_msg = TaskMessage(task_id=payload["task_id"], ...)` 替换为：

```python
        task_msg = core_kafka.parse_event(TOPIC, payload)
```

`parse_event` 返回 `TaskRequest` dataclass，字段与 `TaskMessage` 同名。`process_task(task_msg)` 走 duck-typing。配套：
- `process_task(msg: TaskMessage)` 类型注解改 `TaskRequest`（import 自 apihub_core.events）或去掉。
- grep `TaskMessage` 在 `executor/tests/`，构造处改 `TaskRequest(...)`（同字段）。
- `executor/models.py` 的 `TaskMessage` 若 grep 确认无其它引用则删除；否则标 deprecated 保留。

- [ ] **Step 2: retry task-failures 消费**

`retry/consumer.py:_handle`：保留 `tenant_id = headers.get("tenant_id") or payload.get("tenant_id")` 的 header 优先逻辑（含缺 tenant 的 early-return 校验），然后把 `FailureMessage(...)` 手搓段替换为：

```python
        failure = core_kafka.parse_event(TOPIC, payload)
```

`TaskFailure` 覆盖原 FailureMessage 所需字段。后续 `failure.xxx` 兼容。若 `tenant_id` 需回填进 failure（原逻辑用 header tenant），可 `failure = replace(failure, tenant_id=tenant_id)`（from dataclasses import replace）保证一致。`retry/models.py` 的 `FailureMessage` 若 grep 无其它引用则删除。

- [ ] **Step 3: 跑 executor + retry 现有测试**

```bash
cd services/services/executor && ../../.venv/bin/python -m pytest -q
cd ../retry && ../../.venv/bin/python -m pytest -q
```
Expected: 全绿。retry 的 task-failures 仍手动注入测试消息（R1a 才有真生产者），但现在走 `parse_event`。

- [ ] **Step 4: ruff + commit**

```bash
cd services/services/executor && ../../.venv/bin/ruff check src/executor/consumer.py src/executor/models.py src/executor/processor.py
cd ../retry && ../../.venv/bin/ruff check src/retry_svc/consumer.py src/retry_svc/models.py
git add services/services/executor services/services/retry
git commit -m "refactor: migrate consumers to typed event contract (R0b)"
```

---

## Task 5: 全量回归 + PR

**Files:** 无新增（验证 + PR notes）。

- [ ] **Step 1: 全量回归（.venv 3.11）**

```bash
cd services/libs/apihub-core && ../../.venv/bin/python -m pytest -q
cd ../services/dispatcher && ../../.venv/bin/python -m pytest -q
cd ../executor && ../../.venv/bin/python -m pytest -q
cd ../retry && ../../.venv/bin/python -m pytest -q
cd ../quota && ../../.venv/bin/python -m pytest -q   # quota 未迁，应仍绿
```
Expected: 全绿。

- [ ] **Step 2: grep 确认核心生产走契约（quota 的裸 emit 属已声明 follow-up，不算残留）**

```bash
grep -rn 'kafka.emit("task-requests"\|kafka.emit("task-status"' services/services/dispatcher services/services/executor
grep -rn 'emit_event' services/services/dispatcher services/services/executor services/services/retry
```
Expected: 前者无命中；后者命中各迁移点。

- [ ] **Step 3: PR notes（本 task 不一定有新 commit）**

PR description 必含：
- R0b 交付：typed 事件契约（events.py 4 dataclass）+ emit_event/parse_event + 迁移 dispatcher/executor 生产、executor/retry 消费。
- Follow-up：①quota 的 api-call-events 是 QuotaDecisionEvent（不同形状），未迁——需独立 topic/契约；②notification 的 api-call-events 消费未迁；③task-failures 生产留 R1a。
- 零行为变更；契约 round-trip 单测 + 各服务回归全绿。

---

## Self-Review

1. **Spec coverage**：§2 四事件 → Task 1；§3 helper → Task 2；§4 生产迁移 → Task 3；§4 消费迁移 → Task 4；§5 测试 → 各 Task Step + Task 5。✓
2. **Placeholder scan**：无 TBD。Task 3 Step 2「现有字段组装成 dict d」非占位（字段清单见 spec §2.4，实现者已在文件内）。Task 4「grep 确认无引用再删」是明确指令。✓
3. **Type consistency**：四 dataclass 字段名在 Task 1 定义，Task 3/4 消费一致；`emit_event`/`parse_event` 签名一致；`TOPIC` ClassVar 一致；TaskFailure 必填字段在前（dataclass 规则）。✓
4. **风险**：CallEvent 26 字段易漏 → round-trip 单测兜底；TaskMessage→TaskRequest 触及 executor 测试 → Task 4 Step 1 明确 grep 改；quota 不同形状 → 移出范围记 follow-up。✓
