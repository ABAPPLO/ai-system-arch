"""auth 启动入口。"""


from apihub_core import create_app

from auth.routes import register_routes

# 关键：/v1/apikey/verify 必须在 skip_auth_paths 里
# 它是 APIKey 校验入口，不能递归依赖 APIKey 校验
app = create_app(
    service_name="auth",
    build_routes=register_routes,
    skip_auth_paths=(
        "/health",
        "/metrics",
        "/v1/auth/health",
        "/v1/apikey/verify",  # 内部端点（K8s NetworkPolicy 限制来源）
        "/docs",  # dev 用，prod 关掉
        "/openapi.json",
    ),
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "auth.main:app",
        host="0.0.0.0",
        port=8002,
        workers=2,  # auth 不是热点，2 个够（dispatcher 缓存命中率高）
        log_level="info",
    )
