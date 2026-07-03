"""trace-svc 启动入口。"""

from apihub_core import create_app
from fastapi import FastAPI

from trace_svc.routes import register_routes


def build_app() -> FastAPI:
    return create_app(
        service_name="trace",
        build_routes=register_routes,
        skip_auth_paths=(
            "/health",
            "/metrics",
            "/v1/trace/health",
            "/docs",
            "/openapi.json",
        ),
    )


app = build_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "trace.main:app",
        host="0.0.0.0",
        port=8008,
        workers=2,
        log_level="info",
    )
