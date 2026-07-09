"""trace-svc 路由 —— 调用日志查询。

权限：
  - GET /v1/trace/calls：超管（全部）/ 普通用户（自己租户）
  - GET /v1/trace/calls/{trace_id}：同上
  - GET /v1/trace/calls/stats：同上
"""

from datetime import datetime
from typing import Any

from apihub_core.errors import ApiError, ErrorCode
from apihub_core.tenant import require_tenant
from fastapi import FastAPI, Query

from trace_svc import repository as repo
from trace_svc.models import (
    CallDetail,
    CallListItem,
    CallQuery,
    CallStats,
    CallStatusFilter,
)


def _resolve_query(
    api_id: str | None,
    app_id: str | None,
    trace_id: str | None,
    status: CallStatusFilter,
    since: datetime | None,
    until: datetime | None,
    limit: int,
    offset: int,
) -> CallQuery:
    return CallQuery(
        api_id=api_id,
        app_id=app_id,
        trace_id=trace_id,
        status=status,
        since=since,
        until=until,
        limit=limit,
        offset=offset,
    )


def register_routes(app: FastAPI) -> None:
    @app.get("/v1/trace/calls", response_model=list[CallListItem])
    async def list_calls(
        api_id: str | None = None,
        app_id: str | None = None,
        trace_id: str | None = None,
        status: CallStatusFilter = CallStatusFilter.ALL,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ):
        ctx = require_tenant()
        query = _resolve_query(api_id, app_id, trace_id, status, since, until, limit, offset)

        if ctx.is_platform_admin:
            rows = await repo.list_calls(query, use_admin_session=True)
        else:
            rows = await repo.list_calls(query, viewer_tenant_id=ctx.tenant_id)

        return [_row_to_list_item(r) for r in rows]

    @app.get("/v1/trace/calls/stats", response_model=CallStats)
    async def get_stats(
        api_id: str | None = None,
        app_id: str | None = None,
        status: CallStatusFilter = CallStatusFilter.ALL,
        since: datetime | None = None,
        until: datetime | None = None,
    ):
        ctx = require_tenant()
        query = _resolve_query(api_id, app_id, None, status, since, until, limit=1, offset=0)

        if ctx.is_platform_admin:
            data = await repo.stats(query, use_admin_session=True)
        else:
            data = await repo.stats(query, viewer_tenant_id=ctx.tenant_id)

        return CallStats(**data)

    @app.get("/v1/trace/calls/export")
    async def export_csv():
        """CSV 导出（限 100w 行）—— Phase 2 实现。"""
        require_tenant()
        raise ApiError(
            ErrorCode.INTERNAL,
            "CSV export not yet implemented (Phase 2)",
            http_status=501,
        )

    @app.get("/v1/trace/calls/{trace_id}", response_model=CallDetail)
    async def get_call(trace_id: str):
        ctx = require_tenant()

        if ctx.is_platform_admin:
            row = await repo.get_call(trace_id, use_admin_session=True)
        else:
            row = await repo.get_call(trace_id, viewer_tenant_id=ctx.tenant_id)

        return _row_to_detail(row)

    @app.get("/v1/trace/health")
    async def health():
        return {"status": "ok", "service": "trace"}


# ---------- helpers ----------


def _row_to_list_item(r: dict[str, Any]) -> CallListItem:
    """ClickHouse 行 → CallListItem。

    BOOL 字段 ClickHouse 存 UInt8，转 bool。
    """
    return CallListItem(
        trace_id=str(r.get("trace_id", "")),
        api_id=str(r.get("api_id", "")),
        api_path=str(r.get("path", "")),
        api_method=str(r.get("method", "GET")),
        api_version=str(r.get("api_version_id", "v1")),
        app_id=str(r.get("app_id", "")),
        app_name=None,  # 列已删，恒 None
        caller_ip=_format_ip(r.get("client_ip")),
        http_status=int(r.get("status_code", 0)),
        is_success=bool(r.get("is_success", 0)),
        is_timeout=False,  # 列已删，恒 False
        latency_ms=int(r.get("latency_ms", 0)),
        error_type=r.get("error_code") or None,
        error_msg=r.get("error_msg") or None,
        ts=r.get("ts"),
    )


def _row_to_detail(r: dict[str, Any]) -> CallDetail:
    return CallDetail(
        trace_id=str(r.get("trace_id", "")),
        parent_trace_id=None,
        span_id=None,
        api_id=str(r.get("api_id", "")),
        api_path=str(r.get("path", "")),
        api_method=str(r.get("method", "GET")),
        api_version=str(r.get("api_version_id", "v1")),
        api_mode=None,
        app_id=str(r.get("app_id", "")),
        app_name=None,
        caller_ip=_format_ip(r.get("client_ip")),
        env=None,
        gateway_node=None,
        req_id=r.get("request_id") or None,
        req_size=int(r.get("request_size", 0)) if r.get("request_size") is not None else None,
        resp_size=int(r.get("response_size", 0)) if r.get("response_size") is not None else None,
        gateway_latency_ms=None,
        backend_latency_ms=int(r.get("backend_latency_ms", 0))
        if r.get("backend_latency_ms") is not None
        else None,
        http_status=int(r.get("status_code", 0)),
        is_success=bool(r.get("is_success", 0)),
        is_timeout=False,
        latency_ms=int(r.get("latency_ms", 0)),
        is_streaming=bool(r.get("ai_streaming", 0)),
        token_prompt=int(r.get("token_prompt", 0)) if r.get("token_prompt") is not None else None,
        token_completion=int(r.get("token_completion", 0))
        if r.get("token_completion") is not None
        else None,
        token_total=int(r.get("token_total", 0)) if r.get("token_total") is not None else None,
        ai_model=r.get("ai_model") or None,
        error_type=r.get("error_code") or None,
        error_msg=r.get("error_msg") or None,
        is_retry=False,
        retry_no=None,
        task_id=None,
        ts=r.get("ts"),
    )


def _format_ip(ip: Any) -> str | None:
    if ip is None:
        return None
    if isinstance(ip, str):
        return ip
    return str(ip)
