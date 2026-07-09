"""api-registry 启动入口。"""

from apihub_core import create_app

from api_registry.routes import (
    register_change_request_routes,
    register_routes,
)


def _build(app):
    """注册两组路由。"""
    register_routes(app)
    register_change_request_routes(app)


app = create_app(
    service_name="api-registry",
    build_routes=_build,
    skip_auth_paths=(
        "/health",
        "/metrics",
        "/v1/change-requests/health",
        "/docs",
        "/openapi.json",
    ),
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "api_registry.main:app",
        host="0.0.0.0",
        port=8000,
        workers=4,
        log_level="info",
    )
