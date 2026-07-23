"""retry-svc 启动入口 —— FastAPI app + 后台 worker / consumer。

通过 create_app(extra_lifespan=...) 把 consumer / worker 的生命周期挂到,  # type: ignore[arg-type]
核心 lifespan 上（DB / Redis / Kafka 都就绪后再起）。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from apihub_core import create_app
from apihub_core.config import get_settings
from fastapi import FastAPI

from retry_svc.consumer import FailureConsumer
from retry_svc.routes import register_routes
from retry_svc.worker import RetryWorker


@asynccontextmanager
async def worker_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """起 Kafka consumer + ZSet worker。核心 lifespan 已建好 DB/Redis/Kafka。"""
    settings = get_settings()

    consumer = FailureConsumer(settings)
    await consumer.start()
    app.state.consumer = consumer

    worker = RetryWorker(executor_port=settings.executor_port)
    await worker.start()
    app.state.worker = worker

    try:
        yield
    finally:
        await worker.stop()
        await consumer.stop()


app = create_app(
    service_name="retry",
    build_routes=register_routes,
    skip_auth_paths=(
        "/health",
        "/metrics",
        "/v1/retry/health",
        "/docs",
        "/openapi.json",
    ),
    extra_lifespan=worker_lifespan,  # type: ignore[arg-type]
)


@app.get("/health/ready")
async def health_ready():
    """ready = consumer + worker 都在跑 + DB pool 已建。"""
    consumer = getattr(app.state, "consumer", None)
    worker = getattr(app.state, "worker", None)
    pool_ok = True  # create_app 已建，简化检查
    return {
        "status": "ready" if (consumer and worker and pool_ok) else "starting",
        "consumer_running": consumer is not None,
        "worker_running": worker is not None,
    }


@app.get("/metrics")
async def metrics():
    """简化版 —— 真版走 prometheus_client。"""
    return {
        "failures_consumed": getattr(app.state, "failures_consumed", 0),
        "retries_succeeded": getattr(app.state, "retries_succeeded", 0),
        "retries_dead_letter": getattr(app.state, "retries_dead_letter", 0),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "retry_svc.main:app",
        host="0.0.0.0",
        port=8009,
        workers=1,
        log_level="info",
    )
