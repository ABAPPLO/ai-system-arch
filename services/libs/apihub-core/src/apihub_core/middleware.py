"""FastAPI 中间件 —— 鉴权 + trace + tenant context + 统一错误。"""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from apihub_core import clickhouse as ch
from apihub_core import db, kafka, logging, redis, tracing
from apihub_core.config import get_settings
from apihub_core.errors import ApiError, api_error_handler, unhandled_exception_handler
from apihub_core.tenant import (
    clear_tenant_context,
)


def create_app(
    service_name: str,
    *,
    build_routes: Callable[[FastAPI], None],
    skip_auth_paths: tuple[str, ...] = ("/health", "/metrics"),
    extra_lifespan: Callable[[FastAPI], AsyncIterator[None]] | None = None,
) -> FastAPI:
    """FastAPI 应用工厂 —— 所有微服务统一入口。

    用法：
        from apihub_core import create_app
        app = create_app("api-registry", build_routes=_register_routes)

    extra_lifespan：可选的二次 lifespan，用于后台 worker / Kafka consumer
    等需要在 DB / Redis / Kafka 都就绪后再启动的资源。会在核心 lifespan
    的 yield 之前启动、yield 之后关闭。
    """
    settings = get_settings()
    settings.otel_service_name = service_name

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logging.configure_logging(level=settings.log_level, env=settings.env)
        tracing.configure_tracing(settings)
        await db.init_pool(settings)
        await redis.init_redis(settings)
        await kafka.init_producer(settings)
        ch.init_clickhouse(settings)

        if extra_lifespan is not None:
            async with extra_lifespan(app):
                yield
        else:
            yield

        ch.close_clickhouse()
        await kafka.close_producer()
        await redis.close_redis()
        await db.close_pool()

    app = FastAPI(
        title=service_name,
        lifespan=lifespan,
        docs_url="/docs" if settings.env != "prod" else None,
        redoc_url=None,
    )

    # FastAPIInstrumentor().instrument() 只 patch 未来创建的 app；
    # 当前 app 已经实例化，必须显式 instrument_app(app) 才能让 SERVER span 真正生成。
    # tracer provider 在 lifespan 里 set，instrument_app 在请求到来时懒取，顺序不冲突。
    FastAPIInstrumentor.instrument_app(app)

    build_routes(app)

    # 中间件：注入 tenant context
    @app.middleware("http")
    async def tenant_middleware(request: Request, call_next):
        path = request.url.path
        if path.startswith(skip_auth_paths):
            return await call_next(request)

        # 从 header 取 X-API-Key 或 Authorization: Bearer
        api_key = request.headers.get("X-API-Key") or _extract_bearer(
            request.headers.get("Authorization")
        )
        if not api_key:
            return JSONResponse(
                status_code=401,
                content={"success": False, "code": 10002, "message": "Missing API Key"},
            )

        try:
            from apihub_core.auth import authenticate_request

            await authenticate_request(request, settings, api_key)
        except ApiError as e:
            return api_error_handler(request, e)

        try:
            return await call_next(request)
        finally:
            clear_tenant_context()

    # 错误处理
    @app.exception_handler(ApiError)
    async def api_error_handler_wrapper(request: Request, exc: ApiError):
        return api_error_handler(request, exc)

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception):
        return unhandled_exception_handler(request, exc)

    # 健康检查
    @app.get("/health/live")
    async def health_live():
        return {"status": "alive"}

    @app.get("/health/ready")
    async def health_ready():
        return {"status": "ready"}

    return app


def _extract_bearer(header: str | None) -> str | None:
    if not header:
        return None
    if header.startswith("Bearer "):
        return header[7:]
    return None
