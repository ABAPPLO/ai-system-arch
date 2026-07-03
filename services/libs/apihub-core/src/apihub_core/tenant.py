"""租户上下文（contextvars）— 贯穿 HTTP / Kafka / 日志 / DB session。

详见 docs/11-multi-tenant.md §3 上下文传播。
"""

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Optional

# 全局 contextvar —— asyncio 协程间天然隔离
_tenant_ctx: ContextVar[Optional["TenantContext"]] = ContextVar(
    "apihub_tenant_ctx", default=None
)


@dataclass(frozen=True, slots=True)
class TenantContext:
    """租户上下文 —— 一次请求贯穿全链路。"""

    tenant_id: str
    tenant_type: str           # internal / external / system
    app_id: str | None = None      # 调用方应用 ID
    user_id: str | None = None     # 后台用户 ID
    is_platform_admin: bool = False   # 超管（跨租户）

    @property
    def key_prefix(self) -> str:
        """Redis key 前缀：`t:{tenant_id}:`"""
        return f"t:{self.tenant_id}:"


def set_tenant_context(ctx: TenantContext) -> None:
    _tenant_ctx.set(ctx)


def get_tenant_context() -> TenantContext | None:
    return _tenant_ctx.get()


def require_tenant() -> TenantContext:
    ctx = _tenant_ctx.get()
    if ctx is None:
        from apihub_core.errors import ApiError, ErrorCode
        raise ApiError(
            code=ErrorCode.TENANT_CONTEXT_MISSING,
            message="Tenant context not set on this request",
            http_status=500,
        )
    return ctx


def clear_tenant_context() -> None:
    _tenant_ctx.set(None)
