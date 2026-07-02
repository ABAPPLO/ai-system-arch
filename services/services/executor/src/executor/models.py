"""Kafka 消息 / 内部数据模型。

TaskMessage 是 dispatcher 推到 task-requests 的负载格式（见
dispatcher/src/dispatcher/task_dispatcher.py 的 emit）。
"""


from pydantic import BaseModel, Field


class TaskMessage(BaseModel):
    """dispatcher 推过来的任务请求。"""

    task_id: str = Field(min_length=8)
    api_id: str
    api_version_id: str
    backend_url: str
    payload: str = ""   # 原始请求 body，可能是非 JSON
    timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    callback_url: str | None = None
    # 这些从 Kafka header 取，不在 payload 里 —— processor 会回填
    tenant_id: str | None = None
    app_id: str | None = None
    request_id: str | None = None
    trace_id: str | None = None


class TaskResult(BaseModel):
    """processor 处理结果 —— 给测试断言用。"""

    task_id: str
    status: str           # succeeded / failed / timeout / skipped
    http_status: int | None = None
    error_code: str | None = None
    error_msg: str | None = None
    response_body: str | None = None
    duration_ms: int = 0
