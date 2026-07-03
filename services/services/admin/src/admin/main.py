"""admin-bff 启动入口。

注意：审计 middleware 在 create_app 之后手动注册（因为 create_app 的
tenant_middleware 已经做了鉴权，我们要在那个之后才能拿到上下文）。
"""

from apihub_core import create_app
from fastapi import FastAPI

from admin.routes import install_audit_middleware, register_routes


def build_app() -> FastAPI:
    """构造 app。导出函数让测试用。"""
    app = create_app(
        service_name="admin",
        build_routes=register_routes,
        skip_auth_paths=(
            "/health",
            "/metrics",
            "/v1/admin/health",
            "/docs",
            "/openapi.json",
        ),
    )
    # 审计 middleware 必须在 tenant_middleware 之后注册（FastAPI middleware
    # 是 LIFO：后注册的先执行 inbound、最后执行 outbound —— 我们要在
    # inbound 阶段拿到 tenant context，所以后注册）
    install_audit_middleware(app)
    return app


app = build_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "admin.main:app",
        host="0.0.0.0",
        port=8006,
        workers=2,  # admin QPS 不高，2 副本起（prod 3-5）
        log_level="info",
    )
