"""api-registry 启动入口。"""

from apihub_core import create_app
from api_registry.routes import register_routes

app = create_app(
    service_name="api-registry",
    build_routes=register_routes,
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
