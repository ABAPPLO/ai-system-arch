"""声明式 YAML 的内存模型 —— 对齐 schema/*.yaml 字段。"""

from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator


class BackendType(StrEnum):
    HTTP = "http"
    ASYNC_TASK = "async_task"
    WORKFLOW = "workflow"
    AI_MODEL = "ai_model"


class Method(StrEnum):
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    DELETE = "DELETE"
    PATCH = "PATCH"


class ApiSpec(BaseModel):
    """schema/yaml 中 `api:` 段。"""

    name: str = Field(min_length=2, max_length=64)
    category: str = Field(max_length=32)
    description: str | None = None
    base_path: str = Field(pattern=r"^/[a-z0-9-]+")
    tags: list[str] = []
    owner: str | None = None  # 不入库，仅供 CI/审计


class VersionSpec(BaseModel):
    """schema/yaml 中 `version:` 段。

    与 api_registry.models.ApiVersionCreate 字段对齐；多余字段（method / path /
    retry / rate_limit 等）当前保留在 raw 里，后续随表结构扩展再迁出。
    """

    version: str = Field(pattern=r"^v\d+$")
    backend_type: BackendType = BackendType.HTTP
    backend_url: str
    method: Method = Method.GET
    path: str
    request_schema: dict[str, Any] | None = None
    response_schema: dict[str, Any] | None = None
    masking: dict[str, Any] | None = None
    ai_model: str | None = None
    ai_streaming: bool = False

    @field_validator("backend_url")
    @classmethod
    def _validate_url(cls, v: str) -> str:
        # http(s):// 同步接口、task:// 异步任务、workflow:// Argo 工作流
        allowed = ("http://", "https://", "task://", "workflow://")
        if not v.startswith(allowed):
            raise ValueError(
                f"backend_url must start with one of {allowed}, got {v!r}"
            )
        return v


class ApiDefinition(BaseModel):
    """一份 YAML 文件 = 一个 API + 一个 Version。"""

    api: ApiSpec
    version: VersionSpec
    source_file: str | None = None  # 由 loader 注入，便于错误回报

    def proposed_config(self) -> dict[str, Any]:
        """change_request.proposed_config：把整份 spec 塞进去供审批方看。"""
        return self.model_dump(exclude={"source_file"})


# ============ Loader ============


def load_yaml(path: Path) -> ApiDefinition:
    """读单个 YAML → ApiDefinition。"""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: top-level must be a mapping")
    return _build(raw, source=str(path))


def load_dir(directory: Path) -> list[ApiDefinition]:
    """递归读 directory 下所有 *.yaml / *.yml。"""
    files = sorted(
        [*directory.rglob("*.yaml"), *directory.rglob("*.yml")],
        key=lambda p: str(p),
    )
    return [load_yaml(p) for p in files]


def _build(raw: dict[str, Any], *, source: str) -> ApiDefinition:
    return ApiDefinition.model_validate({**raw, "source_file": source})
