"""docs-svc 响应模型。"""

from typing import Any

from pydantic import BaseModel, Field


class ApiMeta(BaseModel):
    """从 api-registry 读到的 api + 版本元数据。"""

    api_id: str
    api_name: str = Field(min_length=1)
    description: str | None = None
    category: str = ""
    base_path: str
    tags: list[str] = []
    api_status: str

    version_id: str
    version: str  # v1, v2, ...
    backend_type: str = "http"
    backend_url: str
    version_status: str

    request_schema: dict[str, Any] | None = None
    response_schema: dict[str, Any] | None = None
    masking: dict[str, Any] | None = None
    ai_model: str | None = None
    ai_streaming: bool = False


class ExampleResponse(BaseModel):
    """多语言调用示例。"""

    curl: str
    python: str
    javascript: str
    notes: list[str] = Field(
        default=[],
        description="调用方需要知道的注意事项（鉴权 / 流式 / 脱敏 等）",
    )
