"""admin-bff 路由。

3 类端点：
  1. /v1/admin/audit/*  —— 审计查询（list/detail/stats/export）+ 手动 record
  2. /v1/admin/dashboard —— 跨服务聚合概览
  3. /v1/admin/health    —— k8s probe

权限：
  - audit list/detail/stats：超管 OR 同租户成员（RLS 兜底）
  - audit record：内部服务调用（不强制 admin，因为下游服务要写）
  - dashboard：超管 only（聚合数据跨租户）
"""

from datetime import UTC, datetime, timedelta

from apihub_core.errors import ApiError, ErrorCode
from apihub_core.logging import get_logger
from apihub_core.tenant import require_tenant
from fastapi import FastAPI, Request

from admin import repository
from admin.aggregator import get_aggregator
from admin.audit import record_from_request
from admin.models import (
    ArchiveRequest,
    ArchiveResponse,
    AuditDetail,
    AuditListItem,
    AuditQuery,
    AuditRecord,
    AuditStats,
    CleanupRequest,
    CleanupResponse,
    DashboardResponse,
    RecordResponse,
)

log = get_logger(__name__)


def _require_platform_admin():
    """超管 only。"""
    ctx = require_tenant()
    if not ctx.is_platform_admin:
        raise ApiError(
            ErrorCode.FORBIDDEN,
            "platform admin only",
        )
    return ctx


def _resolve_query_params(
    request: Request,
) -> AuditQuery:
    """从 query string 构造 AuditQuery（FastAPI 直接 query params 注入也行，
    但我们想精细控制）。
    """
    q = request.query_params
    try:
        limit = int(q.get("limit", "50"))
        offset = int(q.get("offset", "0"))
    except ValueError as e:
        raise ApiError(ErrorCode.INVALID_PARAMS, "limit/offset must be int") from e

    since_raw = q.get("since")
    until_raw = q.get("until")
    return AuditQuery(
        tenant_id=q.get("tenant_id"),
        actor_id=q.get("actor_id"),
        action=q.get("action"),
        resource_type=q.get("resource_type"),
        resource_id=q.get("resource_id"),
        since=datetime.fromisoformat(since_raw) if since_raw else None,
        until=datetime.fromisoformat(until_raw) if until_raw else None,
        limit=limit,
        offset=offset,
    )


def register_routes(app: FastAPI) -> None:
    # ========== 审计查询 ==========

    @app.get("/v1/admin/audit", response_model=list[AuditListItem])
    async def list_audit(request: Request):
        """列表查询。超管看全部，普通用户只能看自己租户的。"""
        ctx = require_tenant()
        query = _resolve_query_params(request)

        if ctx.is_platform_admin:
            rows = await repository.list_events(query, use_admin_session=True)
        else:
            # 普通用户：强制 tenant_id = 自己
            rows = await repository.list_events(query, viewer_tenant_id=ctx.tenant_id)
        return [AuditListItem(**r) for r in rows]

    @app.get("/v1/admin/audit/stats", response_model=AuditStats)
    async def audit_stats(request: Request):
        """统计 —— top actions / top actors / by_day。"""
        ctx = require_tenant()
        days = int(request.query_params.get("days", "7"))
        days = max(1, min(days, 90))

        if ctx.is_platform_admin:
            data = await repository.stats(use_admin_session=True, days=days)
        else:
            data = await repository.stats(viewer_tenant_id=ctx.tenant_id, days=days)
        return AuditStats(**data)

    @app.get("/v1/admin/audit/{audit_id}", response_model=AuditDetail)
    async def get_audit(audit_id: int):
        """详情（含 detail/IP/UA）。"""
        ctx = require_tenant()
        if ctx.is_platform_admin:
            row = await repository.get_event(audit_id, use_admin_session=True)
        else:
            row = await repository.get_event(audit_id, viewer_tenant_id=ctx.tenant_id)
        return AuditDetail(**row)

    @app.get("/v1/admin/audit/export/csv")
    async def export_csv(request: Request):
        """CSV 导出（Phase 2 完整实现，目前占位返回 501）。"""
        _require_platform_admin()
        raise ApiError(
            ErrorCode.INTERNAL,
            "CSV export not yet implemented (Phase 2)",
            http_status=501,
        )

    # ========== 手动写入（内部服务调用） ==========

    @app.post("/v1/admin/audit/record", response_model=RecordResponse, status_code=201)
    async def record_event(payload: AuditRecord):
        """内部服务调用的写入端点。

        不强制 admin 权限 —— 下游服务（tenant/api-registry/...）用 K8s
        NetworkPolicy 限制来源即可，否则会循环鉴权。
        """
        audit_id = await repository.record(payload)
        return RecordResponse(id=audit_id, recorded=audit_id > 0)

    @app.post("/v1/admin/audit/record-batch", response_model=RecordResponse)
    async def record_batch(payload: list[AuditRecord]):
        """批量写。"""
        n = await repository.record_many(payload)
        return RecordResponse(id=0, recorded=n > 0)

    # ========== Dashboard（跨服务聚合） ==========

    @app.get("/v1/admin/dashboard", response_model=DashboardResponse)
    async def dashboard(request: Request):
        """跨服务聚合 dashboard —— 超管 only。"""
        ctx = _require_platform_admin()
        api_key = request.headers.get("X-API-Key", "")

        agg = get_aggregator()
        tenants_list = await agg.list_tenants(api_key=api_key)

        today_query = AuditQuery(
            tenant_id=ctx.tenant_id if not ctx.is_platform_admin else None,
            since=datetime.now(UTC) - timedelta(days=1),
            limit=1,
        )
        audit_today = await repository.count(
            today_query,
            use_admin_session=ctx.is_platform_admin,
        )
        seven_day_query = AuditQuery(
            tenant_id=ctx.tenant_id if not ctx.is_platform_admin else None,
            since=datetime.now(UTC) - timedelta(days=7),
            limit=1,
        )
        audit_7d = await repository.count(
            seven_day_query,
            use_admin_session=ctx.is_platform_admin,
        )
        recent = await repository.list_events(
            AuditQuery(limit=10), use_admin_session=ctx.is_platform_admin
        )

        return DashboardResponse(
            tenants={
                "total": len(tenants_list),
                "active": sum(1 for t in tenants_list if t.get("status") == "active"),
                "suspended": sum(1 for t in tenants_list if t.get("status") == "suspended"),
                "closed": sum(1 for t in tenants_list if t.get("status") == "closed"),
            },
            audit_today=audit_today,
            audit_7d=audit_7d,
            top_recent_events=[AuditListItem(**r) for r in recent],
        )

    # ========== 健康 ==========

    # ========== 审计归档 ==========

    @app.post("/v1/admin/audit/archive", response_model=ArchiveResponse)
    async def archive(payload: ArchiveRequest):
        """归档超管 only：把早于 before 的审计日志归档到 OSS 并删除。"""
        _require_platform_admin()
        cutoff = payload.before or (datetime.now(UTC) - timedelta(days=180))
        n = await repository.archive_before(cutoff)
        return ArchiveResponse(archived=n, cutoff=cutoff.isoformat())

    # ========== 数据清理 ==========

    @app.post("/v1/admin/data/cleanup", response_model=CleanupResponse)
    async def cleanup(payload: CleanupRequest):
        """清理过期数据。超管 only。"""
        _require_platform_admin()
        now = datetime.now(UTC)
        task_before = now - timedelta(days=payload.task_months * 30)
        retry_before = now - timedelta(days=payload.retry_days)

        partitions = await repository.cleanup_task_partitions(before=task_before)
        retry = await repository.cleanup_retry_tasks(before=retry_before)

        log.info(
            "data_cleanup_done",
            partitions=partitions,
            retry=retry,
            task_months=payload.task_months,
            retry_days=payload.retry_days,
        )
        return CleanupResponse(dropped_partitions=partitions, deleted_retry_tasks=retry)

    # ========== 健康 ==========

    @app.get("/v1/admin/health")
    async def health():
        return {"status": "ok", "service": "admin"}


# ---------- middleware（自动审计 mutation） ----------


def install_audit_middleware(app: FastAPI) -> None:
    """注册自动审计 middleware。

    所有 POST/PUT/PATCH/DELETE 请求结束时调用 record_from_request。
    失败 best-effort，不影响业务。
    """

    @app.middleware("http")
    async def audit_middleware(request: Request, call_next):
        response = await call_next(request)
        # 只审计 mutation，且只审计 /v1/admin/* 下的业务路径
        if (
            request.method.upper() in ("POST", "PUT", "PATCH", "DELETE")
            and request.url.path.startswith("/v1/admin/")
            and not request.url.path.startswith("/v1/admin/health")
        ):
            try:
                await record_from_request(request, status_code=response.status_code)
            except Exception as e:
                log.warning("audit_middleware_failed", error=str(e))
        return response
