"""Kafka 消息 + DB 行 + API schema 数据模型。

task-failures 的负载契约由 apihub_core.events.TaskFailure（typed dataclass）定义，
见 services/libs/apihub-core/src/apihub_core/events.py。
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class RetryStatus(StrEnum):
    """retry_task.status 状态机。

    pending   → 等待延迟队列到期 / 等待手动触发
    running   → worker 正在调用 executor
    ignored   → 后台手动忽略，不再自动重试
    succeeded → 重试成功，闭环
    dead      → 超过 max_attempts，进死信（仍可手动 trigger 复活）
    """

    PENDING = "pending"
    RUNNING = "running"
    IGNORED = "ignored"
    SUCCEEDED = "succeeded"
    DEAD = "dead"


class BackoffPolicy(StrEnum):
    """退避策略 —— 目前只实现 exponential，留 fixed / linear 扩展。"""

    EXPONENTIAL = "exponential"
    FIXED = "fixed"
    LINEAR = "linear"


class RetryTaskRow(BaseModel):
    """retry_task 行视图（list / detail 共用）。"""

    model_config = ConfigDict(coerce_numbers_to_str=True)

    id: int
    tenant_id: str
    trace_id: str
    task_instance_id: str | None = None
    api_id: str
    app_id: str
    max_attempts: int
    retry_count: int
    next_retry_at: datetime | None
    backoff_policy: BackoffPolicy
    backoff_base_ms: int
    status: RetryStatus
    env: str
    last_error_code: str | None = None
    last_error_msg: str | None = None
    last_failed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class RetryAttemptRow(BaseModel):
    """retry_attempt 行视图。"""

    model_config = ConfigDict(coerce_numbers_to_str=True)

    id: int
    tenant_id: str
    retry_task_id: int
    attempt_no: int
    request_body: dict | None = None
    response_status: int | None = None
    response_body: dict | None = None
    error_code: str | None = None
    error_msg: str | None = None
    latency_ms: int | None = None
    attempted_at: datetime


class RetryTaskDetail(RetryTaskRow):
    """详情视图 —— 带每次重试历史。"""

    attempts: list[RetryAttemptRow] = []
    original_request: dict = {}


class ListFailedQuery(BaseModel):
    """GET /admin/retry/failed 查询参数。"""

    model_config = ConfigDict(coerce_numbers_to_str=True)

    since: datetime | None = None
    until: datetime | None = None
    api_id: str | None = None
    app_id: str | None = None
    status: RetryStatus | None = None
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


class RetryStats(BaseModel):
    """重试统计。"""

    total: int = 0
    pending: int = 0
    running: int = 0
    dead: int = 0
    ignored: int = 0
    succeeded: int = 0
    success_rate: float = 0.0
    by_error_code: dict[str, int] = {}
