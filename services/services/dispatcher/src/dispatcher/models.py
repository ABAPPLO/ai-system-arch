"""接口元数据快照（从 api_version 表读出来缓存的精简版）。"""

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel


@dataclass(frozen=True, slots=True)
class ApiVersionSnapshot:
    """dispatcher 转发需要的最小字段集（避免每次查全表）。"""

    id: str  # api_version.id (ver_xxx)
    api_id: str  # 父 API id
    tenant_id: str
    version: str  # v1 / v2
    backend_type: str  # http / async_task / workflow / ai_model
    backend_url: str  # 含 {path_var} 占位符
    method: str
    path: str  # 业务侧 path
    masking: dict[str, Any] | None
    rate_limit: dict[str, Any] | None
    retry_policy: dict[str, Any] | None
    cache_policy: dict[str, Any] | None
    # AI
    ai_model: str | None
    ai_streaming: bool
    ai_params: dict[str, Any] | None
    # SLA
    sla_p99_ms: int | None
    sla_availability: float | None
    # 超时（如果接口配置覆盖）
    timeout_ms: int = 30_000
    # 授权可见性（来自 api.visibility，非 api_version）：public / tenant / private
    # dispatcher 在转发前由 visibility.check_visibility 做应用层授权。
    visibility: str = "private"

    @property
    def is_streaming(self) -> bool:
        return self.backend_type == "ai_model" and self.ai_streaming


class SubmitJobRequest(BaseModel):
    """POST /v1/jobs 请求体（dispatcher 代理到 workflow-svc）。

    用 Pydantic 模型而非裸 request.json()：缺 api_id/app_id/spec 或非 JSON
    时 FastAPI 直接返回 422，而非在 handler 里 KeyError/JSONDecodeError → 500。
    字段对齐 workflow-svc 的 SubmitWorkflowRequest。
    """

    api_id: str
    app_id: str
    spec: dict
    trace_id: str | None = None
    namespace: str = "apihub-workflow"
