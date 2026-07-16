"""api-registry 路由 —— 接口元数据 CRUD + 变更评审工单 + 生命周期管理。

注意：所有 DB 查询都不写 WHERE tenant_id=?，由 RLS 自动过滤。
"""

import uuid

from apihub_core import db, kafka
from apihub_core.errors import ApiError, ErrorCode
from apihub_core.tenant import require_tenant
from fastapi import FastAPI, Query

from api_registry import apisix_client
from api_registry import change_request as cr
from api_registry.models import (
    ApiCreate,
    ApiVersionCreate,
    ApiVersionResponse,
)


def register_routes(app: FastAPI) -> None:
    # ⚠️ 路由顺序：静态段必须在 {param} 之前

    @app.get("/v1/apis")
    async def list_apis(limit: int = 50, offset: int = 0):
        async with db.db_session() as conn:
            rows = await conn.fetch(
                "SELECT * FROM api ORDER BY created_at DESC LIMIT $1 OFFSET $2",
                limit,
                offset,
            )
        return {"items": [dict(r) for r in rows], "limit": limit, "offset": offset}

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
            row = await conn.fetchrow("SELECT * FROM api WHERE id = $1", api_id)
        if not row:
            raise ApiError(ErrorCode.NOT_FOUND, f"API {api_id} not found")
        return dict(row)

    @app.post("/v1/api-versions", response_model=ApiVersionResponse)
    async def create_version(payload: ApiVersionCreate):
        ctx = require_tenant()
        version_id = f"ver_{uuid.uuid4().hex[:16]}"

        async with db.db_session() as conn:
            # 校验 api 属于本租户（RLS 会自动过滤，没查到就是越权或不存在）
            api_row = await conn.fetchrow("SELECT id, name FROM api WHERE id = $1", payload.api_id)
            if not api_row:
                raise ApiError(ErrorCode.API_NOT_FOUND, "API not found")

            await conn.execute(
                """
                INSERT INTO api_version (
                    id, tenant_id, api_id, version, backend_type, backend_url,
                    method, path,
                    request_schema, response_schema, masking,
                    ai_model, ai_streaming,
                    status, created_at
                ) VALUES (
                    $1, $2, $3, $4, $5, $6,
                    $7, $8,
                    $9, $10, $11,
                    $12, $13,
                    'draft', NOW()
                )
                """,
                version_id,
                ctx.tenant_id,
                payload.api_id,
                payload.version,
                payload.backend_type.value,
                payload.backend_url,
                payload.method.value,
                payload.path,
                payload.request_schema,
                payload.response_schema,
                payload.masking,
                payload.ai_model,
                payload.ai_streaming,
            )

            row = await conn.fetchrow("SELECT * FROM api_version WHERE id = $1", version_id)

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
        require_tenant()
        async with db.db_session() as conn:
            row = await conn.fetchrow(
                """
                SELECT v.*, a.base_path
                FROM api_version v JOIN api a ON a.id = v.api_id
                WHERE v.id = $1 AND v.status IN ('draft', 'reviewing')
                """,
                version_id,
            )
            if not row:
                raise ApiError(ErrorCode.API_NOT_PUBLISHED, "Version not publishable")

            # 先下发 APISIX 路由，成功才置 published（避免 DB published 但数据面无路由的窗口）
            await apisix_client.publish_route(
                version_id=version_id,
                method=row["method"],
                path=row["path"],
                base_path=row["base_path"],
            )

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

    @app.post("/v1/api-versions/{version_id}/deprecate")
    async def deprecate_version(version_id: str):
        """标记废弃 —— published → deprecated（仍可调用，给调用方迁移时间）。"""
        require_tenant()
        async with db.db_session() as conn:
            result = await conn.execute(
                """
                UPDATE api_version
                SET status = 'deprecated', deprecated_at = NOW()
                WHERE id = $1 AND status = 'published'
                """,
                version_id,
            )
        if not result.endswith(" 1"):
            raise ApiError(
                ErrorCode.API_NOT_PUBLISHED,
                f"version {version_id} not publishable for deprecate",
                http_status=409,
            )

        await kafka.emit(
            "audit-events",
            {
                "action": "api_version.deprecate",
                "resource_type": "api_version",
                "resource_id": version_id,
            },
        )
        return {"version_id": version_id, "status": "deprecated"}

    @app.post("/v1/api-versions/{version_id}/retire")
    async def retire_version(version_id: str):
        """下线 —— deprecated → retired（不摘除 APISIX 路由；dispatcher 按 status='retired' 返 410 Gone）。

        要求：必须先 deprecated（不能从 published 直接 retire，避免误下线）。
        """
        require_tenant()
        async with db.db_session() as conn:
            result = await conn.execute(
                """
                UPDATE api_version
                SET status = 'retired', retired_at = NOW()
                WHERE id = $1 AND status = 'deprecated'
                """,
                version_id,
            )
        if not result.endswith(" 1"):
            raise ApiError(
                ErrorCode.API_DEPRECATED,
                f"version {version_id} must be deprecated before retire",
                http_status=409,
            )

        # retire 不摘除 APISIX 路由：dispatcher 按 status='retired' 返 410 Gone
        # （避免启用 APISIX serverless 410 插件的 helm upgrade）。stale 路由清理见 follow-up。

        await kafka.emit(
            "audit-events",
            {
                "action": "api_version.retire",
                "resource_type": "api_version",
                "resource_id": version_id,
            },
        )
        return {"version_id": version_id, "status": "retired"}


# ============ 变更评审工单（change_request）============


def register_change_request_routes(app: FastAPI) -> None:
    """单独注册 /v1/change-requests/* 路由，便于路由顺序控制。"""

    # ⚠️ /health 必须在 /{request_id} 之前
    @app.get("/v1/change-requests/health")
    async def health():
        return {"status": "ok", "service": "api-registry"}

    @app.post("/v1/change-requests", status_code=201)
    async def submit_change_request(payload: cr.ChangeRequestCreate):
        """提交变更工单。

        - dev 环境：自助，submit 时 status 自动 = approved（apply 接口直接执行）
        - staging / prod：pending，等 review
        """
        ctx = require_tenant()

        # 提交钉钉审批（stub：dev 返回 None）
        dingtalk_id = await cr.submit_dingtalk_approval(payload)

        req_id = await cr.submit_change_request(
            tenant_id=ctx.tenant_id,
            req=payload,
            dingtalk_approval_id=dingtalk_id,
        )

        # dev 自助：自动 apply
        if payload.target_env == cr.TargetEnv.DEV:
            req = await cr.get_change_request(req_id)
            if req is not None:
                try:
                    await cr.apply_change(req)
                except RuntimeError as e:
                    raise ApiError(
                        ErrorCode.INTERNAL,
                        f"auto-apply failed: {e}",
                        http_status=500,
                    ) from e

        await kafka.emit(
            "audit-events",
            {
                "action": "change_request.submit",
                "resource_type": "change_request",
                "resource_id": req_id,
                "api_id": payload.api_id,
                "change_type": payload.change_type.value,
                "target_env": payload.target_env.value,
            },
        )
        return {
            "request_id": req_id,
            "status": "approved" if payload.target_env == cr.TargetEnv.DEV else "pending",
        }

    @app.get("/v1/change-requests")
    async def list_change_requests(
        api_id: str | None = None,
        status: cr.ChangeRequestStatus | None = None,
        change_type: cr.ChangeType | None = None,
        target_env: cr.TargetEnv | None = None,
        submitted_by: str | None = None,
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ):
        require_tenant()
        query = cr.ListChangeRequestsQuery(
            api_id=api_id,
            status=status,
            change_type=change_type,
            target_env=target_env,
            submitted_by=submitted_by,
            limit=limit,
            offset=offset,
        )
        return await cr.list_change_requests(query)

    @app.get("/v1/change-requests/{request_id}", response_model=cr.ChangeRequest)
    async def get_change_request(request_id: int):
        require_tenant()
        req = await cr.get_change_request(request_id)
        if req is None:
            raise ApiError(
                ErrorCode.NOT_FOUND,
                f"change_request {request_id} not found",
                http_status=404,
            )
        return req

    @app.post("/v1/change-requests/{request_id}/approve")
    async def approve_request(
        request_id: int,
        body: cr.ChangeRequestReview | None = None,
    ):
        ctx = require_tenant()
        # 仅超管可审批（platform_admin 才有 prod 发布权）
        if not ctx.is_platform_admin:
            raise ApiError(
                ErrorCode.FORBIDDEN,
                "only platform admin can approve",
                http_status=403,
            )

        ok = await cr.approve_change_request(
            request_id,
            reviewed_by=ctx.user_id or "unknown",
            review_comment=body.review_comment if body else None,
        )
        if not ok:
            raise ApiError(
                ErrorCode.NOT_FOUND,
                f"change_request {request_id} not found or not pending",
                http_status=404,
            )
        return {"request_id": request_id, "status": "approved"}

    @app.post("/v1/change-requests/{request_id}/reject")
    async def reject_request(
        request_id: int,
        body: cr.ChangeRequestReview | None = None,
    ):
        ctx = require_tenant()
        if not ctx.is_platform_admin:
            raise ApiError(
                ErrorCode.FORBIDDEN,
                "only platform admin can reject",
                http_status=403,
            )

        ok = await cr.reject_change_request(
            request_id,
            reviewed_by=ctx.user_id or "unknown",
            review_comment=body.review_comment if body else None,
        )
        if not ok:
            raise ApiError(
                ErrorCode.NOT_FOUND,
                f"change_request {request_id} not found or not pending",
                http_status=404,
            )
        return {"request_id": request_id, "status": "rejected"}

    @app.post("/v1/change-requests/{request_id}/cancel")
    async def cancel_request(request_id: int):
        ctx = require_tenant()
        ok = await cr.cancel_change_request(request_id, submitted_by=ctx.user_id or "")
        if not ok:
            raise ApiError(
                ErrorCode.NOT_FOUND,
                f"change_request {request_id} not found, not pending, or not yours",
                http_status=404,
            )
        return {"request_id": request_id, "status": "cancelled"}

    @app.post("/v1/change-requests/{request_id}/apply")
    async def apply_request(request_id: int):
        """approved → applied（执行实际副作用，如发布/下线）。

        幂等：再次 apply 已 applied 的请求返回 409。
        """
        require_tenant()
        req = await cr.get_change_request(request_id)
        if req is None:
            raise ApiError(
                ErrorCode.NOT_FOUND,
                f"change_request {request_id} not found",
                http_status=404,
            )
        if req.status != cr.ChangeRequestStatus.APPROVED:
            raise ApiError(
                ErrorCode.CONFLICT,
                f"change_request {request_id} status={req.status.value}, expected=approved",
                http_status=409,
            )

        try:
            summary = await cr.apply_change(req)
        except RuntimeError as e:
            raise ApiError(
                ErrorCode.INTERNAL,
                f"apply failed: {e}",
                http_status=500,
            ) from e

        await kafka.emit(
            "audit-events",
            {
                "action": "change_request.apply",
                "resource_type": "change_request",
                "resource_id": request_id,
                "summary": summary,
            },
        )
        return {"request_id": request_id, "status": "applied", "summary": summary}
