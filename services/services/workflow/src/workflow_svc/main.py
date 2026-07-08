"""workflow-svc 启动入口。

HTTP server 给 admin / portal UI 用，提交工作流、查状态、流式日志。
不消费 Kafka（与 executor / retry-svc 不同）。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from apihub_core import create_app
from apihub_core.config import get_settings
from fastapi import FastAPI

from workflow_svc import argo_client
from workflow_svc.routes import register_routes


@asynccontextmanager
async def argo_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """init argo client；mode 由 settings.argo_mode 决定。"""
    settings = get_settings()
    mode = getattr(settings, "argo_mode", "stub")
    init_kwargs = {}
    if mode == "k8s":
        # K8sArgoClient 会自动读 SA token
        init_kwargs = {
            "api_server": getattr(settings, "k8s_api_server",
                                   "https://kubernetes.default.svc"),
        }
    argo_client.init_argo_client(mode=mode, **init_kwargs)

    try:
        yield
    finally:
        await argo_client.close_argo_client()


app = create_app(
    service_name="workflow",
    build_routes=register_routes,
    skip_auth_paths=(
        "/health",
        "/metrics",
        "/v1/workflows/health",
        "/docs",
        "/openapi.json",
    ),
    extra_lifespan=argo_lifespan,
)


@app.get("/health/ready")
async def health_ready():
    """ready = argo client 初始化完成。"""
    inited = argo_client._client is not None  # noqa: SLF001
    return {
        "status": "ready" if inited else "starting",
        "argo_client_ready": inited,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "workflow_svc.main:app",
        host="0.0.0.0",
        port=8010,
        workers=2,
        log_level="info",
    )
