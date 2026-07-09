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
from pydantic import BaseModel, Field

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
    docs_url=None,  # worker 服务不开 Swagger
    redoc_url=None,
)


@app.get("/health/live")
async def health_live():
    return {"status": "alive"}


@app.get("/health/ready")
async def health_ready():
    """ready = consumer 在跑 + DB pool 已建。"""
    consumer = getattr(app.state, "consumer", None)
    pool_ok = db._pool is not None  # noqa: SLF001
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


# ============ 内部接口：retry-svc worker 调用 ============


class RetryRequest(BaseModel):
    """retry-svc worker 推过来的重试请求（见 retry_svc/worker.py::_call_executor）。"""

    task_id: str
    backend_url: str
    payload: str = ""
    tenant_id: str
    api_id: str
    app_id: str = ""
    trace_id: str = ""
    request_id: str = ""
    timeout_seconds: float = Field(default=30.0, gt=0, le=600)


@app.post("/v1/internal/retry")
async def internal_retry(req: RetryRequest):
    """同步重试一个 task —— POST backend_url，按 status 判定 succeeded/failed/timeout。

    设计上和 processor._call_backend 一致，但不写 PG 状态机（PG 由 retry-svc 自己维护，
    executor 在这条内部路径上只做"调一次后端"）。
    """
    import time

    from executor.processor import _client  # 复用进程级 httpx 单例

    if _client is None:
        return {
            "succeeded": False,
            "error_code": "http_client_not_init",
            "error_msg": "executor http client not initialized",
            "latency_ms": 0,
        }

    started = time.monotonic()
    headers = {
        "Content-Type": "application/json",
        "X-Task-Id": req.task_id,
        "X-Request-Id": req.request_id,
        "X-Tenant-Id": req.tenant_id,
        "X-Trace-Id": req.trace_id,
    }

    try:
        resp = await _client.post(
            req.backend_url,
            content=req.payload.encode("utf-8") if req.payload else b"",
            headers=headers,
            timeout=req.timeout_seconds,
        )
    except Exception as e:
        latency_ms = int((time.monotonic() - started) * 1000)
        return {
            "succeeded": False,
            "error_code": "backend_unreachable",
            "error_msg": f"{type(e).__name__}: {e}",
            "latency_ms": latency_ms,
        }

    latency_ms = int((time.monotonic() - started) * 1000)
    ok = 200 <= resp.status_code < 300
    return {
        "succeeded": ok,
        "status": resp.status_code,
        "body": resp.json()
        if resp.headers.get("content-type", "").startswith("application/json")
        else resp.text,
        "error_code": None if ok else f"backend_http_{resp.status_code}",
        "error_msg": "" if ok else (resp.text or "")[:500],
        "latency_ms": latency_ms,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "executor.main:app",
        host="0.0.0.0",
        port=8003,
        workers=1,  # worker 进程靠 K8s 副本扩，单进程多 consumer 不显式并发
        log_level="info",
    )
