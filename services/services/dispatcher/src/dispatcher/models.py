"""接口元数据快照（从 api_version 表读出来缓存的精简版）。"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ApiVersionSnapshot:
    """dispatcher 转发需要的最小字段集（避免每次查全表）。"""

    id: str                       # api_version.id (ver_xxx)
    api_id: str                   # 父 API id
    tenant_id: str
    version: str                  # v1 / v2
    backend_type: str             # http / async_task / workflow / ai_model
    backend_url: str              # 含 {path_var} 占位符
    method: str
    path: str                     # 业务侧 path
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

    @property
    def is_streaming(self) -> bool:
        return self.backend_type == "ai_model" and self.ai_streaming
