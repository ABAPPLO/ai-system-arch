"""executor 启动入口 —— FastAPI app 主要给 k8s probe 用，worker 在 lifespan 里。

worker pattern：进程启动 → 起 Kafka consumer + httpx client → 持续拽消息。
HTTP server 只暴露 /health/* 用于 liveness/readiness probe。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from apihub_core import (
    create_app,  # noqa: F401  (用来复用 health/error handler)
    db,
    kafka,
    redis,
)
from apihub_core.config import get_settings
from fastapi import FastAPI

from executor.consumer import TaskConsumer
from executor.processor import close_http_client, init_http_client
from executor.repository import reset_stale_running


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动顺序：DB pool → Redis → Kafka producer（推 task-status 用）
    → httpx client → reset stale running → Kafka consumer。"""
    settings = get_settings()
    settings.otel_service_name = "executor"

    await db.init_pool(settings)
    await redis.init_redis(settings)
    await kafka.init_producer(settings)
    await init_http_client()

    # 启动时把上次崩溃残留的 running 任务重置为 pending（不动 succeeded/failed）
    n = await reset_stale_running(timeout_seconds=600)
    if n:
        app.state.stale_reset_count = n

    consumer = TaskConsumer(settings)
    await consumer.start()
    app.state.consumer = consumer

    try:
        yield
    finally:
        await consumer.stop()
        await close_http_client()
        await kafka.close_producer()
        await redis.close_redis()
        await db.close_pool()


app = FastAPI(
    title="executor",
    lifespan=lifespan,
    docs_url=None,   # worker 服务不开 Swagger
    redoc_url=None,
)


@app.get("/health/live")
async def health_live():
    return {"status": "alive"}


@app.get("/health/ready")
async def health_ready():
    """ready = consumer 在跑 + DB pool 已建。"""
    consumer = getattr(app.state, "consumer", None)
    pool_ok = db._pool is not None   # noqa: SLF001
    return {
        "status": "ready" if (consumer and pool_ok) else "starting",
        "consumer_running": consumer is not None,
        "db_pool_ready": pool_ok,
    }


@app.get("/metrics")
async def metrics():
    """简化版 —— 真版走 prometheus_client。"""
    return {
        "tasks_processed": getattr(app.state, "tasks_processed", 0),
        "stale_reset": getattr(app.state, "stale_reset_count", 0),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "executor.main:app",
        host="0.0.0.0",
        port=8003,
        workers=1,   # worker 进程靠 K8s 副本扩，单进程多 consumer 不显式并发
        log_level="info",
    )
