"""API 元数据模型（与 PG 表 api / api_version 对齐）。

详见 docs/04-data-model.md §1。
"""

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class ApiStatus(str, Enum):
    DRAFT      = "draft"
    REVIEWING  = "reviewing"
    PUBLISHED  = "published"
    DEPRECATED = "deprecated"
    RETIRED    = "retired"


class BackendType(str, Enum):
    HTTP      = "http"
    ASYNC_TASK = "async_task"
    WORKFLOW  = "workflow"
    AI_MODEL  = "ai_model"  # ADR-004 AI 网关预留


class ApiCreate(BaseModel):
    name: str = Field(min_length=2, max_length=64)
    description: Optional[str] = None
    category: str = Field(max_length=32)
    base_path: str = Field(pattern=r"^/[a-z0-9-]+")
    tags: list[str] = []


class ApiVersionCreate(BaseModel):
    api_id: str
    version: str = Field(pattern=r"^v\d+$")  # v1, v2, ...
    backend_type: BackendType = BackendType.HTTP
    backend_url: str
    request_schema: Optional[dict[str, Any]] = None
    response_schema: Optional[dict[str, Any]] = None
    masking: Optional[dict[str, Any]] = None  # 字段脱敏规则
    # AI 字段（backend_type=ai_model 时）
    ai_model: Optional[str] = None
    ai_streaming: bool = False


class ApiVersionResponse(BaseModel):
    id: str
    api_id: str
    version: str
    backend_type: BackendType
    backend_url: str
    status: ApiStatus
    ai_model: Optional[str] = None
    ai_streaming: bool = False
    created_at: datetime
    published_at: Optional[datetime] = None
