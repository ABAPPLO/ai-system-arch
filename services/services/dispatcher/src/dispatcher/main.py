"""dispatcher 启动入口。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from apihub_core import create_app, get_settings
from apihub_core import identity as identity_mod
from apihub_core.l1 import TTLCache
from apihub_core.logging import get_logger
from fastapi import FastAPI

from dispatcher import resolver as resolver_mod
from dispatcher.forwarder import HttpForwarder
from dispatcher.routes import register_routes, set_forwarder

log = get_logger(__name__)


def _build_routes(app: FastAPI) -> None:
    # lifespan 在 create_app 里已注册（apihub_core 的 init/close）
    # 我们额外挂个 forwarder 初始化
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan_with_httpclient(_app: FastAPI) -> AsyncIterator[None]:
        settings = get_settings()
        async with original_lifespan(_app):
            # L1 进程内缓存：identity（API key / hmac secret）+ resolver snapshot。
            # create_app lifespan 已在此 async with 入口完成 Redis 初始化，
            # 故此处视为“Redis 就绪后”；yield 前启用、shutdown（finally）清理。
            if settings.dispatcher_l1_enabled:
                _ttl = settings.dispatcher_l1_ttl_seconds
                _max = settings.dispatcher_l1_maxsize
                identity_mod.configure_l1(
                    identity=TTLCache(maxsize=_max, ttl=_ttl),
                    secret=TTLCache(maxsize=_max, ttl=_ttl),
                )
                resolver_mod.configure_snapshot_l1(TTLCache(maxsize=_max, ttl=_ttl))
            client = httpx.AsyncClient(
                # 显式超时：connect/pool/write 有界防连接耗尽；read 给 300s
                # 兼容 AI SSE 慢生成（同步转发另有 per-request timeout=snap.timeout_ms 覆盖）
                timeout=httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=30.0),
                limits=httpx.Limits(
                    max_connections=500,
                    max_keepalive_connections=100,
                    keepalive_expiry=30,
                ),
                http2=True,
            )
            set_forwarder(HttpForwarder(client))
            # workflow 代理专用 client（与 forwarder 隔离，timeout 更短，
            # 因为只是转发到 workflow-svc，不承担 AI SSE 长连接）
            workflow_client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=10.0),
            )
            app.state.workflow_client = workflow_client
            log.info("dispatcher_ready")
            try:
                yield
            finally:
                # shutdown：关闭 L1 缓存（configure_l1(None) 即恢复“未启用”态，
                # 与 startup 的 enable 对称；即便 l1 未启用，传 None 也是安全 no-op）
                identity_mod.configure_l1(identity=None, secret=None)
                resolver_mod.configure_snapshot_l1(None)
                await workflow_client.aclose()
                await client.aclose()

    app.router.lifespan_context = lifespan_with_httpclient
    register_routes(app)


app = create_app(
    service_name="dispatcher",
    build_routes=_build_routes,
    skip_auth_paths=(
        "/health",
        "/metrics",
        "/v1/dispatcher/health",  # 自身健康检查
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
