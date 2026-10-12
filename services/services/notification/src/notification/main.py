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
            # 内部服务调用（auth 注册发验证邮件等）：无用户凭证，
            # 走 require_ingress_auth（fail-closed）+ payload 显式 tenant_id
            "/v1/internal/notify/send",
            "/v1/internal/notify/batch",
        ),
        extra_lifespan=start_consumer,
    )


app = build_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("notification.main:app", host="0.0.0.0", port=8012, workers=2, log_level="info")
