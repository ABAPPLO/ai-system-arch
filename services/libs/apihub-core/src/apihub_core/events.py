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
