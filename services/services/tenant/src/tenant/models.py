"""请求 / 响应模型。"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

# ---------- 租户 ----------

VALID_TYPES = ("internal", "external", "system")
VALID_STATUSES = ("active", "suspended", "closed")
VALID_TIERS = ("free", "standard", "premium")


class TenantCreate(BaseModel):
    """创建租户（仅超管）。"""

    id: str = Field(min_length=2, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    name: str = Field(min_length=1, max_length=128)
    slug: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9-]+$")
    type: str = Field(default="internal")
    parent_id: str | None = None
    tier: str = Field(default="standard")
    metadata: dict[str, Any] = Field(default_factory=dict)

    def normalized_type(self) -> str:
        return self.type if self.type in VALID_TYPES else "internal"

    def normalized_tier(self) -> str:
        return self.tier if self.tier in VALID_TIERS else "standard"


class TenantUpdate(BaseModel):
    """更新租户 —— 只允许改 name/slug/tier/metadata（type/status 走专用端点）。"""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    slug: str | None = Field(default=None, min_length=2, max_length=64)
    tier: str | None = None
    metadata: dict[str, Any] | None = None


class TenantResponse(BaseModel):
    """对外响应 —— 普通用户看到 masked name，超管看完整。"""

    id: str
    name: str
    slug: str
    type: str
    status: str
    tier: str
    parent_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


# ---------- 成员 ----------

VALID_ROLES = ("owner", "admin", "developer", "viewer")


class MemberAdd(BaseModel):
    user_id: str = Field(min_length=1)
    role: str = Field(default="developer")


class MemberUpdate(BaseModel):
    role: str


class MemberResponse(BaseModel):
    id: str
    tenant_id: str
    user_id: str
    role: str
    created_at: datetime


# ---------- 配额 ----------


class QuotaConfig(BaseModel):
    """tenant.metadata.quota 字段的标准格式。

    - day_limit: 每日调用上限（0 = 不限）
    - rate_limit: {second, minute, day} per-app/api 三层（参考 quota 服务）
    """

    day_limit: int = Field(default=0, ge=0)
    rate_limit: dict[str, Any] = Field(default_factory=dict)


class UsageResponse(BaseModel):
    """当日用量（聚合 quota 服务的输出）。"""

    tenant_id: str
    day_used: int = 0
    day_limit: int = 0
    remaining: int = 0
