from apihub_core import create_app
from billing.routes import router


def _build(app):
    app.include_router(router)


app = create_app(service_name="billing", build_routes=_build, skip_auth_paths=("/health", "/metrics", "/docs", "/openapi.json"))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("billing.main:app", host="0.0.0.0", port=8014, workers=1, log_level="info")
