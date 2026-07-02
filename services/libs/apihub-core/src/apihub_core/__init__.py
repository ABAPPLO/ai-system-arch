"""APIHub 共享基础库。

公共组件：
- 租户上下文（contextvars）
- RLS 数据库会话
- Redis 租户前缀客户端
- Kafka 事件投递
- 结构化日志（structlog + OTel trace 关联）
- 鉴权（APIKey / JWT）
- 统一错误模型
- FastAPI 应用工厂
"""

from apihub_core.config import Settings, get_settings
from apihub_core.tenant import (
    TenantContext,
    get_tenant_context,
    set_tenant_context,
    clear_tenant_context,
    require_tenant,
)
from apihub_core.errors import (
    ApiError,
    ErrorCode,
    ErrorResponse,
    api_error_handler,
)
from apihub_core.middleware import create_app

__version__ = "0.1.0"

__all__ = [
    "Settings",
    "get_settings",
    "TenantContext",
    "get_tenant_context",
    "set_tenant_context",
    "clear_tenant_context",
    "require_tenant",
    "ApiError",
    "ErrorCode",
    "ErrorResponse",
    "api_error_handler",
    "create_app",
]
