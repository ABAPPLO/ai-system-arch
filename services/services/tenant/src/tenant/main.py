"""tenant-svc 启动入口。"""

from apihub_core import create_app

from tenant.routes import register_routes

app = create_app(
    service_name="tenant",
    build_routes=register_routes,
    skip_auth_paths=(
        "/health",
        "/metrics",
        "/v1/tenant/health",
        "/docs",
        "/openapi.json",
    ),
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "tenant.main:app",
        host="0.0.0.0",
        port=8005,
        workers=2,  # tenant-svc 不是热点，3-5 副本够（docs/03 §3.14）
        log_level="info",
    )
