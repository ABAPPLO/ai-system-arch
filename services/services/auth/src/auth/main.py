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
        # /internal/auth/check: APISIX tenant-affinity 调用，body 里带 api_key，
        # 与 /v1/apikey/verify 同属内部跨租户校验入口（R0a §3.10）
        "/internal/auth/check",
        # R2e: dispatcher 冷路径取 HMAC secret（集群内 NetworkPolicy 限制来源，
        # 同 /v1/apikey/verify 的冷回源语义）
        "/v1/internal/hmac-secret",
        # 外部开发者身份端点：注册/验证/登录发生在鉴权之前（无凭证），必须跳过 APIKey middleware
        "/v1/auth/register",
        "/v1/auth/verify-email",
        "/v1/auth/login",
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
