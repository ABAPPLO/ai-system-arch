"""dispatcher 启动入口。"""

from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
from fastapi import FastAPI

from apihub_core import create_app
from apihub_core.logging import get_logger

from dispatcher.forwarder import HttpForwarder
from dispatcher.routes import register_routes, set_forwarder

log = get_logger(__name__)


def _build_routes(app: FastAPI) -> None:
    # lifespan 在 create_app 里已注册（apihub_core 的 init/close）
    # 我们额外挂个 forwarder 初始化
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan_with_httpclient(_app: FastAPI) -> AsyncIterator[None]:
        async with original_lifespan(_app):
            client = httpx.AsyncClient(
                timeout=None,
                limits=httpx.Limits(
                    max_connections=500,
                    max_keepalive_connections=100,
                    keepalive_expiry=30,
                ),
                http2=True,
            )
            set_forwarder(HttpForwarder(client))
            log.info("dispatcher_ready")
            try:
                yield
            finally:
                await client.aclose()

    app.router.lifespan_context = lifespan_with_httpclient
    register_routes(app)


app = create_app(
    service_name="dispatcher",
    build_routes=_build_routes,
    skip_auth_paths=(
        "/health",
        "/metrics",
        "/v1/dispatcher/health",   # 自身健康检查
    ),
)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "dispatcher.main:app",
        host="0.0.0.0",
        port=8001,
        workers=4,
        log_level="info",
    )
