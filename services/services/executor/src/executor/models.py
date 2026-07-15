"""内部数据模型。

task-requests 的负载契约由 apihub_core.events.TaskRequest（typed dataclass）定义，
见 services/libs/apihub-core/src/apihub_core/events.py。
"""

from pydantic import BaseModel


class TaskResult(BaseModel):
    """processor 处理结果 —— 给测试断言用。"""

    task_id: str
    status: str  # succeeded / failed / timeout / skipped
    http_status: int | None = None
    error_code: str | None = None
    error_msg: str | None = None
    response_body: str | None = None
    duration_ms: int = 0
