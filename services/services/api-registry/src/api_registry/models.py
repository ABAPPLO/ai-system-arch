"""API 元数据模型（与 PG 表 api / api_version 对齐）。

详见 docs/04-data-model.md §1。
"""

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ApiStatus(StrEnum):
    DRAFT = "draft"
    REVIEWING = "reviewing"
    PUBLISHED = "published"
    DEPRECATED = "deprecated"
    RETIRED = "retired"


class BackendType(StrEnum):
    HTTP = "http"
    ASYNC_TASK = "async_task"
    WORKFLOW = "workflow"
    AI_MODEL = "ai_model"  # ADR-004 AI 网关预留


class Method(StrEnum):
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    DELETE = "DELETE"
    PATCH = "PATCH"


class ApiCreate(BaseModel):
    name: str = Field(min_length=2, max_length=64)
    description: str | None = None
    category: str = Field(max_length=32)
    base_path: str = Field(pattern=r"^/[a-z0-9-]+")
    tags: list[str] = []


class ApiUpdate(BaseModel):
    """PATCH /v1/apis/{id} —— 部分更新。

    base_path 不可变（不在字段集里）；model_config extra='forbid' → 调用方传
    任何额外字段（含 base_path）直接 422。
    """

    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=2, max_length=64)
    description: str | None = None
    category: str | None = Field(default=None, max_length=32)
    tags: list[str] | None = None
    visibility: Literal["private", "tenant", "public"] | None = None


class ApiVersionCreate(BaseModel):
    api_id: str
    version: str = Field(pattern=r"^v\d+$")  # v1, v2, ...
    backend_type: BackendType = BackendType.HTTP
    backend_url: str
    method: Method = Method.GET
    path: str
    request_schema: dict[str, Any] | None = None
    response_schema: dict[str, Any] | None = None
    masking: dict[str, Any] | None = None  # 字段脱敏规则
    # AI 字段（backend_type=ai_model 时）
    ai_model: str | None = None
    ai_streaming: bool = False


class ApiVersionResponse(BaseModel):
    id: str
    api_id: str
    version: str
    backend_type: BackendType
    backend_url: str
    method: Method
    path: str
    status: ApiStatus
    ai_model: str | None = None
    ai_streaming: bool = False
    created_at: datetime
    published_at: datetime | None = None
