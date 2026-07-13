"""AI 网关启动入口。"""

from apihub_core import create_app

from ai_gateway.routes import router


def _build(app):
    app.include_router(router)


app = create_app(
    service_name="ai-gateway",
    build_routes=_build,
    skip_auth_paths=(
        "/health",
        "/metrics",
        "/docs",
        "/openapi.json",
        "/v1/chat/completions",
    ),
)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "ai_gateway.main:app",
        host="0.0.0.0",
        port=8013,
        workers=1,
        log_level="info",
    )
