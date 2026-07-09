"""工作流数据模型 —— API schema + Argo CRD 投影。"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class WorkflowStatus(StrEnum):
    """workflow_instance.status 状态机（与 Argo Workflow 阶段对齐）。"""

    SUBMITTED = "submitted"  # 已提交 Argo，未开始
    RUNNING = "running"  # Argo 已开始执行
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"  # 手动 cancel
    UNKNOWN = "unknown"  # Argo 不可达 / 状态解析失败


class StepStatus(StrEnum):
    """单个 step 的状态。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    UNKNOWN = "unknown"


class WorkflowTemplateRef(BaseModel):
    """模板引用（step → template）。"""

    template: str
    arguments: dict[str, str] = {}


class WorkflowStep(BaseModel):
    """工作流中的一个 step（对应 Argo 节点）。"""

    name: str
    template: str
    status: StepStatus = StepStatus.PENDING
    started_at: datetime | None = None
    finished_at: datetime | None = None
    message: str | None = None  # 错误 / 进度备注
    inputs: dict[str, str] = {}
    outputs: dict[str, str] = {}


class SubmitWorkflowRequest(BaseModel):
    """POST /v1/workflows 请求体。

    spec 是用户原始 Argo Workflow spec（templates / entrypoint / arguments）。
    workflow-svc 不解析 DAG 内部细节，只是把它包成 CRD 提交。
    """

    api_id: str
    app_id: str
    trace_id: str
    spec: dict = Field(..., description="Argo Workflow spec")
    namespace: str = Field(default="apihub-workflows")


class Workflow(BaseModel):
    """工作流实例 —— 列表 / 详情共用，详情带 steps。"""

    id: int
    tenant_id: str
    workflow_uuid: str
    argo_name: str
    namespace: str
    api_id: str
    app_id: str
    trace_id: str
    spec: dict
    status: WorkflowStatus
    submitted_at: datetime
    finished_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    message: str | None = None


class WorkflowDetail(Workflow):
    """详情视图 —— 含每个 step 状态。"""

    steps: list[WorkflowStep] = []


class WorkflowListItem(BaseModel):
    """列表项（精简版，不带 spec / steps）。"""

    id: int
    tenant_id: str
    workflow_uuid: str
    argo_name: str
    api_id: str
    app_id: str
    trace_id: str
    status: WorkflowStatus
    submitted_at: datetime
    finished_at: datetime | None = None


class ListWorkflowsQuery(BaseModel):
    """GET /v1/workflows 查询参数。"""

    api_id: str | None = None
    app_id: str | None = None
    trace_id: str | None = None
    status: WorkflowStatus | None = None
    since: datetime | None = None
    until: datetime | None = None
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


class LogChunk(BaseModel):
    """单个 step 的日志片段（SSE 一帧）。"""

    step_name: str
    line: str
    timestamp: datetime | None = None
