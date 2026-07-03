"""docs-svc 启动入口。"""

from apihub_core import create_app
from fastapi import FastAPI

from docs.routes import register_routes


def build_app() -> FastAPI:
    return create_app(
        service_name="docs",
        build_routes=register_routes,
        skip_auth_paths=(
            "/health",
            "/metrics",
            "/v1/docs/health",
            "/docs",
            "/openapi.json",
        ),
    )


app = build_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "docs.main:app",
        host="0.0.0.0",
        port=8007,
        workers=2,
        log_level="info",
    )
