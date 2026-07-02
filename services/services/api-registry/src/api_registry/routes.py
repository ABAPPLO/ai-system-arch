"""api-registry 路由 —— 接口元数据 CRUD。

注意：所有 DB 查询都不写 WHERE tenant_id=?，由 RLS 自动过滤。
"""

import uuid
from datetime import datetime, timezone

from fastapi import FastAPI

from apihub_core import db, kafka
from apihub_core.errors import ApiError, ErrorCode
from apihub_core.tenant import require_tenant

from api_registry.models import (
    ApiCreate,
    ApiVersionCreate,
    ApiVersionResponse,
)


def register_routes(app: FastAPI) -> None:

    @app.post("/v1/apis", response_model=dict)
    async def create_api(payload: ApiCreate):
        ctx = require_tenant()
        api_id = f"api_{uuid.uuid4().hex[:16]}"

        async with db.db_session() as conn:
            await conn.execute(
                """
                INSERT INTO api (
                    id, tenant_id, name, description, category,
                    base_path, tags, status, created_at, updated_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, 'draft', NOW(), NOW())
                """,
                api_id,
                ctx.tenant_id,
                payload.name,
                payload.description,
                payload.category,
                payload.base_path,
                payload.tags,
            )

        await kafka.emit(
            "audit-events",
            {
                "action": "api.create",
                "resource_type": "api",
                "resource_id": api_id,
                "detail": payload.model_dump(),
            },
        )
        return {"api_id": api_id}

    @app.get("/v1/apis/{api_id}")
    async def get_api(api_id: str):
        async with db.db_session() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM api WHERE id = $1", api_id
            )
        if not row:
            raise ApiError(ErrorCode.NOT_FOUND, f"API {api_id} not found")
        return dict(row)

    @app.post("/v1/api-versions", response_model=ApiVersionResponse)
    async def create_version(payload: ApiVersionCreate):
        ctx = require_tenant()
        version_id = f"ver_{uuid.uuid4().hex[:16]}"

        async with db.db_session() as conn:
            # 校验 api 属于本租户（RLS 会自动过滤，没查到就是越权或不存在）
            api_row = await conn.fetchrow(
                "SELECT id, name FROM api WHERE id = $1", payload.api_id
            )
            if not api_row:
                raise ApiError(ErrorCode.API_NOT_FOUND, "API not found")

            await conn.execute(
                """
                INSERT INTO api_version (
                    id, tenant_id, api_id, version, backend_type, backend_url,
                    request_schema, response_schema, masking,
                    ai_model, ai_streaming,
                    status, created_at
                ) VALUES (
                    $1, $2, $3, $4, $5, $6,
                    $7, $8, $9,
                    $10, $11,
                    'draft', NOW()
                )
                """,
                version_id,
                ctx.tenant_id,
                payload.api_id,
                payload.version,
                payload.backend_type.value,
                payload.backend_url,
                payload.request_schema,
                payload.response_schema,
                payload.masking,
                payload.ai_model,
                payload.ai_streaming,
            )

            row = await conn.fetchrow(
                "SELECT * FROM api_version WHERE id = $1", version_id
            )

        await kafka.emit(
            "audit-events",
            {
                "action": "api_version.create",
                "resource_type": "api_version",
                "resource_id": version_id,
                "api_id": payload.api_id,
                "version": payload.version,
            },
        )
        return ApiVersionResponse(**dict(row))

    @app.post("/v1/api-versions/{version_id}/publish")
    async def publish_version(version_id: str):
        """发布版本 —— 走分级审批流（详见 docs/05-core-flows.md §3）。

        Phase 1 简化：直接状态置为 published，并下发 APISIX 路由。
        Phase 2 起接钉钉审批工单。
        """
        ctx = require_tenant()
        async with db.db_session() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM api_version WHERE id = $1 AND status IN ('draft', 'reviewing')",
                version_id,
            )
            if not row:
                raise ApiError(ErrorCode.API_NOT_PUBLISHED, "Version not publishable")

            # TODO: 实际下发 APISIX 路由（通过 admin API 或 etcd）
            # from api_registry.apisix_client import publish_route
            # await publish_route(row)

            await conn.execute(
                "UPDATE api_version SET status = 'published', published_at = NOW() WHERE id = $1",
                version_id,
            )

        await kafka.emit(
            "audit-events",
            {
                "action": "api_version.publish",
                "resource_type": "api_version",
                "resource_id": version_id,
            },
        )
        return {"version_id": version_id, "status": "published"}

    @app.get("/v1/apis")
    async def list_apis(limit: int = 50, offset: int = 0):
        async with db.db_session() as conn:
            rows = await conn.fetch(
                "SELECT * FROM api ORDER BY created_at DESC LIMIT $1 OFFSET $2",
                limit,
                offset,
            )
        return {"items": [dict(r) for r in rows], "limit": limit, "offset": offset}
