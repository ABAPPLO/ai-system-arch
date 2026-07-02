"""quota 启动入口。"""


from apihub_core import create_app

from quota.routes import register_routes

app = create_app(
    service_name="quota",
    build_routes=register_routes,
    skip_auth_paths=(
        "/health",
        "/metrics",
        "/v1/quota/health",
        # /v1/quota/check 是 dispatcher 等内部服务调，靠 K8s NetworkPolicy 限制来源
        # （和 auth/verify 同样模式）
        "/v1/quota/check",
        "/v1/quota/check-strict",
        "/v1/quota/refund",
        "/docs",
        "/openapi.json",
    ),
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "quota.main:app",
        host="0.0.0.0",
        port=8004,
        workers=4,   # quota 是延迟敏感热点，多 worker 充分利用多核
        log_level="info",
    )
