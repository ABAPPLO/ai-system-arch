"""docs-svc 路由 —— OpenAPI spec + 多语言示例。

文档查询是无状态的，所有写操作（接口发布）在 api-registry。
本服务对外不强制鉴权（spec 是接口发布后可公开的信息）—— 但读 api
元数据走 db_session，RLS 自动按租户过滤。
"""

from typing import Any

import yaml
from apihub_core.tenant import require_tenant
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

from docs import examples as examples_mod
from docs import openapi_gen
from docs import repository as repo


def register_routes(app: FastAPI) -> None:

    @app.get("/v1/docs/apis/{api_id}/openapi.json")
    async def get_openapi_json(api_id: str, request: Request, version: str | None = None):
        """OpenAPI 3.0 spec（JSON 格式）。"""
        meta = await repo.get_api_meta(api_id, version=version)
        base_url = str(request.base_url).rstrip("/")
        spec = openapi_gen.build_openapi_spec(meta, base_url=base_url)
        return spec

    @app.get(
        "/v1/docs/apis/{api_id}/openapi.yaml",
        response_class=PlainTextResponse,
    )
    async def get_openapi_yaml(api_id: str, request: Request, version: str | None = None):
        """OpenAPI 3.0 spec（YAML 格式）。"""
        meta = await repo.get_api_meta(api_id, version=version)
        base_url = str(request.base_url).rstrip("/")
        spec = openapi_gen.build_openapi_spec(meta, base_url=base_url)
        return yaml.safe_dump(
            spec,
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
        )

    @app.get("/v1/docs/apis/{api_id}/examples")
    async def get_examples(api_id: str, request: Request, version: str | None = None):
        """多语言调用示例（curl / Python / JavaScript）。"""
        meta = await repo.get_api_meta(api_id, version=version)
        base_url = str(request.base_url).rstrip("/")
        return examples_mod.build_examples(meta, base_url=base_url)

    @app.get("/v1/docs/apis/{api_id}/versions")
    async def list_versions(api_id: str) -> dict[str, Any]:
        """列某 API 的全部版本（前端切版本用）。"""
        ctx = require_tenant()
        items = await repo.list_versions(api_id)
        return {"api_id": api_id, "viewer_tenant_id": ctx.tenant_id, "items": items}

    @app.get("/v1/docs/health")
    async def health():
        return {"status": "ok", "service": "docs"}
