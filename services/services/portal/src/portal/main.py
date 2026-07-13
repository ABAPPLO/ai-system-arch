"""portal-bff 启动入口 —— 外部开发者门户聚合层（薄代理 + app/key 自助）。"""

from apihub_core import create_app

from portal.routes import register_routes

app = create_app(
    service_name="portal",
    build_routes=register_routes,
    skip_auth_paths=(
        "/health",
        "/metrics",
        "/v1/portal/auth/register",
        "/v1/portal/auth/verify-email",
        "/v1/portal/auth/login",
        "/docs",
        "/openapi.json",
    ),
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "portal.main:app",
        host="0.0.0.0",
        port=8011,
        workers=2,
        log_level="info",
    )
