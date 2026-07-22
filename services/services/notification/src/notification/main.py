"""notification-svc 启动入口 —— Webhook 推送 + Kafka 消费者。"""

from apihub_core import create_app

from notification import routes
from notification.consumer import start_consumer


def build_app():
    return create_app(
        service_name="notification",
        build_routes=routes.register_routes,
        skip_auth_paths=(
            "/health",
            "/metrics",
            "/docs",
            "/openapi.json",
        ),
        extra_lifespan=start_consumer,
    )


app = build_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("notification.main:app", host="0.0.0.0", port=8012, workers=2, log_level="info")
