# R0b Kafka 事件契约 设计

日期：2026-07-15
状态：Draft（待 review）
依据：[apihub-fix-program-design.md](2026-07-15-apihub-fix-program-design.md) §5 Wave 0 R0b；[phase4-audit-findings.md](../../phase4-audit-findings.md) §9-D（Kafka 无契约是 retry 断链的架构根因）。
范围决策：**全量迁移**——4 个 topic 的现有生产/消费全部迁到 typed 契约（用户 2026-07-15 拍板）。

---

## 1. 背景与目标

现状：`apihub_core.kafka.emit(topic, dict)` 是松散的——每个生产者各自手搓 payload（`build_call_event` / `TaskMessage` / ad-hoc dict），字段在生产/消费两端独立维护。这正是 `task-failures` 链断裂（executor 从不产、retry 一直等）能长期不被发现的架构根因（§9-D）。

R0b 目标：在 `apihub_core` 建立**单一事实源**的 typed 事件契约 + 强制 helper，让生产/消费都走它。R0b 是 R1a（救 retry 断链）的前置——R1a 让 executor 产 `task-failures` 时，直接用 R0b 的 `TaskFailure` 契约。

成功标准：4 个 topic 的所有现有生产/消费都走 `emit_event`/`parse_event`；契约 round-trip 单测通过；各服务现有测试不回归；**R0b 自身零行为变更**（同样事件在流，只是变 typed）。

## 2. 契约：`apihub_core/events.py`

机制：`@dataclass(frozen=True, slots=True)`（对齐 `TenantContext` 风格——轻量、mypy 可查、`dataclasses.asdict` 可序列化，不引 pydantic 重依赖）。每个事件一个 dataclass + 一个 `TOPIC` 常量。

### 2.1 `TaskRequest` → topic `task-requests`（owner: dispatcher → executor）
字段（来自现有 `executor/models.py::TaskMessage`，**保持一致**）：
```
task_id: str
api_id: str
api_version_id: str
backend_url: str
payload: str = ""              # 原始请求 body，可能非 JSON
timeout_seconds: float = 30.0
callback_url: str | None = None
# 以下从 Kafka header 取（kafka.emit 自动注入 tenant_id/app_id；request_id/trace_id 走 extra_headers）
tenant_id: str | None = None
app_id: str | None = None
request_id: str | None = None
trace_id: str | None = None
```
> 迁移期保留 `executor.models.TaskMessage`（pydantic）作为消费端解析模型，或直接换用 dataclass + 手动校验——见 §4 决策。

### 2.2 `TaskStatus` → topic `task-status`（owner: executor → observability/notifier）
字段（来自 `executor/processor.py:88` 现有 emit）：
```
task_id: str
tenant_id: str
app_id: str
api_id: str
status: str            # succeeded / failed / timeout
error_code: str = ""
duration_ms: int = 0
request_id: str = ""   # 现行走 extra_headers，契约里统一进 payload（消费更简单）
```

### 2.3 `TaskFailure` → topic `task-failures`（owner: executor → retry）⚠️ R0b 只定义，不生产
字段（来自 `retry/models.py::FailureMessage` / `retry/consumer.py:_handle` 期望）：
```
task_id: str
tenant_id: str
app_id: str = ""
api_id: str
api_version_id: str | None = None
trace_id: str = ""
request_id: str = ""
backend_url: str
payload: str = ""
error_code: str = "unknown"
error_msg: str = ""
timeout_seconds: float = 30.0
max_attempts: int = 3
backoff_base_ms: int = 1000
```
> R0b 定义此契约 + 把 retry consumer 迁去 `parse_event` 它；**executor 生产 task-failures 留给 R1a**。

### 2.4 `CallEvent` → topic `api-call-events`（owner: dispatcher/quota → CH/notification）
字段（来自 `dispatcher/event.py::build_call_event`，**逐字保留**——ClickHouse schema 对齐）：
```
ts: str                  # CH DateTime64(3) 格式 'YYYY-MM-DD HH:MM:SS.mmm'（生成时填）
tenant_id: str           # CH 需从 payload 读（CH 无 header 概念），故保留在 payload
tenant_type: str
app_id: str
api_id: str
api_version_id: str
trace_id: str
request_id: str
method: str
path: str
status_code: int
is_success: int          # 0/1（CH 用 UInt8，不 bool）
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
```
> `CallEvent` 的 `ts/trace_id/request_id` 生成逻辑（`_now_ch_ts`/`_gen_*`）保留：把现有 `build_call_event(...)` 改成返回 `CallEvent` dataclass（内部仍调那几个 helper），消费端/notification 不变。

## 3. 强制 helper（`apihub_core/kafka.py` 增量）

- **`emit_event(event) -> None`**：收 typed dataclass → `payload = asdict(event)` → 复用现有 `emit(topic=event.TOPIC, payload, key=..., extra_headers=...)`。**保留**现有 tenant/traceparent/PRODUCER-span 注入逻辑。`event.TOPIC` 由 dataclass 的 `ClassVar` 提供。
- **`parse_event(topic, payload: dict) -> <Event dataclass>`**：按 topic 路由到对应 dataclass 构造；**容忍多余字段**（只取已知字段，未知字段忽略）→ 向前兼容，避免 CH 加列打挂消费。
- 旧 `emit(topic, dict)` **保留**（api-registry 的 7 个 emit 若是别的 topic，不在 R0b 范围，继续用旧 API）。
- `consume_with_trace` 不变（trace 注入已对）。

## 4. 迁移范围（全量，4 topic）

**生产侧：**
- `dispatcher/task_dispatcher.py:49`（task-requests）→ `emit_event(TaskRequest(...))`
- `dispatcher/forwarder.py:286/311/349` + `dispatcher/event.py`（api-call-events）→ `build_call_event` 返回 `CallEvent`，`emit_event(call_event)`
- `executor/processor.py:88`（task-status）→ `emit_event(TaskStatus(...))`
- `quota/routes.py:62`（api-call-events）→ `emit_event(CallEvent(...))`

**消费侧：**
- `executor/consumer.py`（task-requests）→ `parse_event("task-requests", msg.value)` 得 `TaskRequest`（替换现有 TaskMessage 直构或保留 TaskMessage 作 pydantic 兜底）
- `retry/consumer.py:_handle`（task-failures）→ `parse_event("task-failures", payload)` 得 `TaskFailure`（替换现有 FailureMessage 手搓字段）
- `notification/consumer.py`（api-call-events）→ 若需要字段，用 `parse_event`；当前只透传 webhook，影响小

**决策（§2.1）：** `TaskMessage`(pydantic) 与 `TaskRequest`(dataclass) 二选一。推荐**消费端改用 dataclass `TaskRequest`**，删掉重复的 pydantic `TaskMessage`（DRY）——但若 executor 现有测试强依赖 pydantic 校验，则保留 `TaskMessage` 作 `parse_event` 的内部实现。实现期由 TDD 决定，spec 不强锁。

## 5. 测试

- **契约 round-trip 单测**（`apihub-core/tests/test_events.py`，纯单测无 PG/Redis）：每个事件 `Event(...) → asdict → parse_event → == 原 Event`；多余字段被忽略；必填字段缺失抛错。
- **迁移点不回归**：dispatcher(test_event/test_jobs)、executor(test_processor/test_consumer)、retry(test_consumer)、quota、notification 现有测试全绿。
- `task-failures` 仍无生产者——R0b 不改这点（R1a 才补），retry consumer 测试继续用手动注入（但走新 `parse_event`）。

## 6. 风险

- **CallEvent 字段多（26 个）**：迁移易漏字段 → 靠 round-trip 单测 + CH schema 对齐断言兜底。
- **`is_success`/`ai_streaming` 是 int(0/1) 不是 bool**：dataclass 字段类型用 `int`（与 CH UInt8 对齐），别误改成 bool。
- **容忍多余字段** vs **严格**：选容忍（向前兼容），但加一条"未知字段记 debug log"便于发现 schema 漂移。
- **api-registry 的 7 个 emit**：先 grep 确认 topic；若是 change/audit-events（非 4 topic 之一），明确不在 R0b 范围。

## 7. 不做（R0b 边界）

- 不加 executor→task-failures 生产（R1a）。
- 不动 tenant-on-consume 自动还原（§9-D 的另一半，属服务边界 R0c）。
- 不动 api-registry 的非 4-topic emit。
- 不引 schema registry / Avro / Protobuf（YAGNI，dataclass + JSON 够）。

## 8. 下一步

R0b spec 经用户复核通过后 → 调 **writing-plans** 出 TDD 逐步实施计划 → subagent 驱动实现 → 一个 squash-PR。
