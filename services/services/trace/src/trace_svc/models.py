"""trace-svc 查询模型。

ClickHouse 表 api_call_log 的 tenant_id/api_id/app_id 均为 String；
trace_id/path/method 也是字符串。响应字段全部用 str/bool。
部分字段（span_id/retry_no/...）在精简 schema 中无对应列，恒为 None/默认，
保留在模型里以维持 API 契约向后兼容。
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class CallStatusFilter(StrEnum):
    ALL = "all"
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"


class CallQuery(BaseModel):
    """列表查询参数 —— 普通用户 tenant_id 强制按 viewer_tenant_id 过滤。"""

    api_id: str | None = None
    app_id: str | None = None
    trace_id: str | None = None
    status: CallStatusFilter = CallStatusFilter.ALL
    since: datetime | None = None
    until: datetime | None = None
    limit: int = Field(default=50, ge=1, le=500)
    offset: int = Field(default=0, ge=0)


class CallListItem(BaseModel):
    """列表项 —— 不带 detail/headers（太大）。"""

    trace_id: str
    api_id: str
    api_path: str
    api_method: str
    api_version: str
    app_id: str
    app_name: str | None = None
    caller_ip: str | None = None
    http_status: int
    is_success: bool
    is_timeout: bool
    latency_ms: int
    error_type: str | None = None
    error_msg: str | None = None
    ts: datetime


class CallDetail(CallListItem):
    """详情 —— 含 token / 重试 / 上下文。"""

    parent_trace_id: str | None = None
    span_id: str | None = None
    api_mode: str | None = None
    env: str | None = None
    gateway_node: str | None = None
    req_id: str | None = None
    req_size: int | None = None
    resp_size: int | None = None
    gateway_latency_ms: int | None = None
    backend_latency_ms: int | None = None
    is_streaming: bool = False
    token_prompt: int | None = None
    token_completion: int | None = None
    token_total: int | None = None
    ai_model: str | None = None
    is_retry: bool = False
    retry_no: int | None = None
    task_id: str | None = None


class CallStats(BaseModel):
    """聚合统计 —— 大盘用。"""

    total: int
    success_count: int
    failed_count: int
    timeout_count: int
    success_rate: float
    p50_latency_ms: float
    p95_latency_ms: float
    p99_latency_ms: float
    avg_latency_ms: float
    qps: float
    top_apis: list[dict[str, Any]] = Field(
        default_factory=list,
        description="[{api_id, api_path, n, success_rate, p95_latency_ms}, ...]",
    )
    by_hour: list[dict[str, Any]] = Field(
        default_factory=list,
        description="[{hour: 'YYYY-MM-DD HH:00:00', n, success_rate}, ...]",
    )
