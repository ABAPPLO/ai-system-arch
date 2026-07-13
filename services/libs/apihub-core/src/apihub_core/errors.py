"""统一错误模型 — 对外契约一致。

详见 docs/01-architecture.md §3.5 对外契约统一。
"""

from enum import IntEnum
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


class ErrorCode(IntEnum):
    # 通用 1xxxx
    INVALID_PARAMS = 10001
    UNAUTHORIZED = 10002
    FORBIDDEN = 10003
    NOT_FOUND = 10004
    RATE_LIMITED = 10005
    CONFLICT = 10006  # 状态机非法转换 / 唯一约束冲突
    INVALID_INPUT = 10007  # 语义级输入错误（如 token 失效/过期），区别于 INVALID_PARAMS（字段格式校验）
    INTERNAL = 10000

    # 租户 2xxxx
    TENANT_NOT_FOUND = 20001
    TENANT_DISABLED = 20002
    TENANT_CONTEXT_MISSING = 20003
    TENANT_QUOTA_EXCEEDED = 20004

    # 接口 3xxxx
    API_NOT_FOUND = 30001
    API_NOT_PUBLISHED = 30002
    API_DEPRECATED = 30003
    API_DOWN = 30004

    # 任务 4xxxx
    TASK_NOT_FOUND = 40001
    TASK_FAILED = 40002
    TASK_TIMEOUT = 40003


_HTTP_STATUS_MAP = {
    ErrorCode.INVALID_PARAMS: 400,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.CONFLICT: 409,
    ErrorCode.INVALID_INPUT: 400,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.TENANT_QUOTA_EXCEEDED: 429,
    ErrorCode.TENANT_NOT_FOUND: 404,
    ErrorCode.TENANT_DISABLED: 403,
    ErrorCode.TENANT_CONTEXT_MISSING: 500,
    ErrorCode.API_NOT_FOUND: 404,
    ErrorCode.API_NOT_PUBLISHED: 404,
    ErrorCode.API_DEPRECATED: 410,
    ErrorCode.API_DOWN: 503,
    ErrorCode.TASK_NOT_FOUND: 404,
    ErrorCode.TASK_TIMEOUT: 504,
    ErrorCode.INTERNAL: 500,
}


class ApiError(Exception):
    def __init__(
        self,
        code: ErrorCode,
        message: str,
        http_status: int | None = None,
        details: dict[str, Any] | None = None,
    ):
        self.code = code
        self.message = message
        self.http_status = http_status or _HTTP_STATUS_MAP.get(code, 500)
        self.details = details or {}
        super().__init__(f"[{code.name}] {message}")


class ErrorResponse(JSONResponse):
    pass


def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    from apihub_core.logging import get_logger

    log = get_logger(__name__)
    log.warning(
        "api_error",
        code=exc.code.name,
        message=exc.message,
        path=str(request.url.path),
        details=exc.details,
    )
    return JSONResponse(
        status_code=exc.http_status,
        content={
            "success": False,
            "code": exc.code.value,
            "message": exc.message,
            "details": exc.details,
            # trace_id 由 OTel 注入到响应 Header，不写 body（避免泄漏内部信息）
        },
    )


def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    from apihub_core.logging import get_logger

    log = get_logger(__name__)
    log.exception("unhandled_exception", path=str(request.url.path), error=str(exc))
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "code": ErrorCode.INTERNAL.value,
            "message": "Internal Server Error",
        },
    )
