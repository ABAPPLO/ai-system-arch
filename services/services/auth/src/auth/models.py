"""请求 / 响应模型。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field


class AppCreate(BaseModel):
    """创建 app 请求（portal 转发；调用方 tenant 来自中间件 JWT/APIKey ctx）。"""

    name: str = Field(min_length=2, max_length=64)
    # 与 app.type 的 DB CHECK 约束对齐（init-db/01-schema.sql）：
    # 非法值在 Pydantic 边界即 422，避免 asyncpg CheckViolationError → 500。
    type: Literal["internal", "external", "web", "mobile", "server"] = "external"


class AppResponse(BaseModel):
    """app 响应（字段对齐 portal 契约）。"""

    id: str
    name: str
    tenant_id: str
    type: str
    status: str


class ApiKeyCreate(BaseModel):
    """创建 APIKey 请求（管理员操作）。"""

    name: str = Field(min_length=2, max_length=64)
    scopes: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None
    # R2e：True = 该 key 走 HMAC 签名模式（平台验签 + secret 加密存）
    signing: bool = False


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
    # R2e：HMAC 签名 secret 明文（仅 signing=True 创建时返回一次），否则 None
    hmac_secret: str | None = None


class HmacSecretRequest(BaseModel):
    """dispatcher 冷路径取 HMAC secret 请求（集群内 /v1/internal/hmac-secret）。"""

    key_id: str = Field(min_length=5)


class HmacSecretResponse(BaseModel):
    """HMAC secret 响应 —— None = key 未 enrolled（非错）。"""

    hmac_secret: str | None


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
    signing: bool = False  # = hmac_secret_encrypted IS NOT NULL；前端据此 gate rotate


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
    # R2e: enrolled key 不可走 bearer（authenticate_request 据此拒）；key_id 供 _verify_hmac nonce_key
    hmac_enrolled: bool = False
    key_id: str | None = None


# ---------- 外部开发者身份（注册 / 登录）----------


class RegisterRequest(BaseModel):
    """外部开发者注册请求。"""

    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    phone: str = Field(min_length=4, max_length=20)
    name: str = Field(min_length=1, max_length=64)


class LoginRequest(BaseModel):
    """外部开发者登录请求。"""

    email: EmailStr
    password: str


class AuthResponse(BaseModel):
    """登录成功响应。"""

    access_token: str
    refresh_token: str = ""
    expires_in: int = 7200
    user: dict


class RefreshRequest(BaseModel):
    """刷新 token 请求。"""

    refresh_token: str


class DingTalkCallbackRequest(BaseModel):
    """钉钉 SSO 回调请求（前端 LoginCallback 提交 code+state）。"""

    code: str = Field(min_length=1)
    state: str = Field(min_length=1)


class DeleteAccountResponse(BaseModel):
    """账号删除响应。"""

    user_id: str
    status: str = "deleted"


class ConsentItem(BaseModel):
    """单条同意记录。"""

    purpose: str
    description: str = ""
    status: str
    granted_at: str
    updated_at: str


class ConsentResponse(BaseModel):
    """同意列表响应。"""

    consents: list[ConsentItem]


class ConsentWithdrawResponse(BaseModel):
    """同意撤回响应。"""

    user_id: str
    status: str = "withdrawn"


class ExportResponse(BaseModel):
    """个人数据导出响应（GDPR Right to portability）。"""

    user_id: str
    exported_at: str
    account: dict
    tenants: list[dict] = Field(default_factory=list)
    apps: list[dict] = Field(default_factory=list)
    api_keys: list[dict] = Field(default_factory=list)
    billing_records: list[dict] = Field(default_factory=list)
