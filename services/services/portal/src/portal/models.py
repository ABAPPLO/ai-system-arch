"""portal Pydantic 模型 —— app/key 自助用的请求/响应形状。"""

from typing import Any

from pydantic import BaseModel, Field


class AppCreate(BaseModel):
    name: str = Field(min_length=2, max_length=64)
    type: str = "external"


class AppResponse(BaseModel):
    id: str
    name: str
    tenant_id: str
    type: str
    status: str


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=2, max_length=64)


class ApiKeyResponse(BaseModel):
    id: str
    app_id: str
    name: str
    key_prefix: str
    api_key: str  # 明文仅此次返回


class PortalApiItem(BaseModel):
    """API 目录列表项（Portal 公开字段，隐藏 backend_url 等内部信息）。"""

    api_id: str
    name: str
    description: str | None = None
    category: str = ""
    tags: list[str] = []
    base_path: str
    visibility: str = "public"
    backend_type: str = "http"
    version: str = ""
    updated_at: str = ""


class PortalApiListResponse(BaseModel):
    items: list[PortalApiItem]
    total: int
    limit: int
    offset: int
    categories: list[str] = []
    tags: list[str] = []


class PortalVersionItem(BaseModel):
    """API 版本详情（不含 backend_url——仅服务端知道）。"""

    version_id: str
    version: str
    method: str
    path: str
    backend_type: str = "http"
    status: str
    request_schema: dict[str, Any] | None = None
    response_schema: dict[str, Any] | None = None
    masking: dict[str, Any] | None = None
    ai_model: str | None = None
    ai_streaming: bool = False


class PortalApiDetail(BaseModel):
    api_id: str
    name: str
    description: str | None = None
    category: str = ""
    tags: list[str] = []
    base_path: str
    visibility: str = "public"
    api_status: str
    versions: list[PortalVersionItem] = []


class TryRequest(BaseModel):
    """在线调试请求体。api_key 只在服务端传递。"""

    api_id: str
    version_id: str | None = None  # 不传则用最新 published
    method: str = "GET"
    path_params: dict[str, str] = {}
    query_params: dict[str, str] = {}
    headers: dict[str, str] = {}
    body: Any = None  # JSON body
    api_key: str  # 调用者的 API Key
    timeout_ms: int = 30000


class TryResponse(BaseModel):
    """在线调试响应。HTTP 永远 200，真实 status 在字段内。"""

    status: int
    headers: dict[str, str] = {}
    body: Any = None
    latency_ms: int = 0
    error: str | None = None


class PlanInfo(BaseModel):
    code: str
    name: str
    description: str | None = None
    price_cents: int = 0
    quota_included: dict = {}
    rate_limits: dict = {}
    features: dict | None = None
    sort_order: int = 0


class SubscriptionInfo(BaseModel):
    plan_code: str
    plan_name: str = ""
    period_start: str = ""
    period_end: str = ""
    status: str = ""
    auto_renew: bool = True
