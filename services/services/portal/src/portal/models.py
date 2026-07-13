"""portal Pydantic 模型 —— app/key 自助用的请求/响应形状。"""

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
