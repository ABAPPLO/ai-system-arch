"""dispatcher 路由 —— 单一 catch-all 入口。

入口：ANY /dispatch/{rest:path}
解析：优先 X-API-Version-Id header，回退 path 匹配
"""

import httpx
from fastapi import FastAPI, Request

from apihub_core.errors import ApiError, ErrorCode
from apihub_core.logging import get_logger

from dispatcher.forwarder import HttpForwarder
from dispatcher.resolver import resolve_by_header, resolve_by_path
from dispatcher.task_dispatcher import dispatch_async_task

log = get_logger(__name__)

# 模块级 forwarder（init_app 时注入 client）
_forwarder: HttpForwarder | None = None


def set_forwarder(f: HttpForwarder) -> None:
    global _forwarder
    _forwarder = f


def get_forwarder() -> HttpForwarder:
    if _forwarder is None:
        raise RuntimeError("Forwarder not initialized")
    return _forwarder


def register_routes(app: FastAPI) -> None:

    @app.api_route(
        "/dispatch/{rest:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    )
    async def dispatch(request: Request):
        rest = request.path_params["rest"]
        method = request.method

        # 解析接口元数据
        version_id = request.headers.get("X-API-Version-Id")
        if version_id:
            snap = await resolve_by_header(version_id)
        else:
            full_path = f"/{rest}"
            snap = await resolve_by_path(method, full_path)

        # 按 backend_type 分流
        if snap.backend_type == "async_task":
            return await dispatch_async_task(snap, request)

        if snap.backend_type == "workflow":
            raise ApiError(
                ErrorCode.INTERNAL,
                "workflow backend not yet supported (Phase 2)",
                http_status=501,
            )

        # http / ai_model
        return await get_forwarder().forward(snap, request)

    @app.get("/v1/dispatcher/health")
    async def health():
        return {"status": "ok", "service": "dispatcher"}
