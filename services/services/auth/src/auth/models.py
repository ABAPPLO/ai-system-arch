"""请求 / 响应模型。"""

from datetime import datetime

from pydantic import BaseModel, Field


class ApiKeyCreate(BaseModel):
    """创建 APIKey 请求（管理员操作）。"""

    name: str = Field(min_length=2, max_length=64)
    scopes: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None


class ApiKeyResponse(BaseModel):
    """创建成功响应 —— 明文只在这里出现一次。"""

    id: str
    app_id: str
    name: str
    scopes: list[str]
    api_key: str  # ⚠️ 明文，仅创建时返回
    display_prefix: str  # 后续列表只展示这个
    expires_at: datetime | None = None
    created_at: datetime


class ApiKeyListItem(BaseModel):
    """列表项 —— 不含明文。"""

    id: str
    app_id: str
    name: str
    scopes: list[str]
    display_prefix: str
    status: str
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    created_at: datetime
    revoked_at: datetime | None = None


class VerifyRequest(BaseModel):
    """内部服务调用的校验请求（dispatcher 等调用）。"""

    api_key: str = Field(min_length=10)


class VerifyResponse(BaseModel):
    """校验响应 —— dispatcher 据此设置 TenantContext。"""

    is_active: bool
    tenant_id: str
    tenant_type: str
    app_id: str
    is_platform_admin: bool = False
    scopes: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None
