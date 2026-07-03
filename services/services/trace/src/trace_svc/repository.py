"""trace-svc ClickHouse 查询 —— 调用日志列表 / 详情 / 聚合。

租户隔离：ClickHouse 无 RLS，每个查询 WHERE 子句都强制 tenant_id。
普通用户 viewer_tenant_id 必填；超管 use_admin_session=True 可跨租户。
"""

from typing import Any

from apihub_core import clickhouse as ch
from apihub_core.errors import ApiError, ErrorCode
from apihub_core.logging import get_logger

from trace_svc.models import CallQuery, CallStatusFilter

log = get_logger(__name__)


# ---------- WHERE 构造 ----------


def _build_where(
    query: CallQuery,
    *,
    viewer_tenant_id: str | None,
) -> tuple[str, dict[str, Any]]:
    """构造 WHERE 子句 + 参数。

    tenant_id 强制：
      - viewer_tenant_id 给了 → 必须等于该值（防越权）
      - 没给（admin 视角）→ 不过滤 tenant（看全部）
    """
    clauses: list[str] = []
    params: dict[str, Any] = {}

    if viewer_tenant_id is not None:
        clauses.append("tenant_id = %(tenant_id)s")
        params["tenant_id"] = int(viewer_tenant_id) if viewer_tenant_id.isdigit() else 0

    if query.api_id:
        clauses.append("api_uuid = %(api_id)s")
        params["api_id"] = query.api_id
    if query.app_id:
        clauses.append("app_uuid = %(app_id)s")
        params["app_id"] = query.app_id
    if query.trace_id:
        clauses.append("trace_id = %(trace_id)s")
        params["trace_id"] = query.trace_id
    if query.since:
        clauses.append("ts >= %(since)s")
        params["since"] = query.since.strftime("%Y-%m-%d %H:%M:%S")
    if query.until:
        clauses.append("ts < %(until)s")
        params["until"] = query.until.strftime("%Y-%m-%d %H:%M:%S")

    if query.status == CallStatusFilter.SUCCESS:
        clauses.append("is_success = 1")
    elif query.status == CallStatusFilter.FAILED:
        clauses.append("is_success = 0")
    elif query.status == CallStatusFilter.TIMEOUT:
        clauses.append("is_timeout = 1")

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


# ---------- 列表 ----------


_LIST_COLUMNS = """
    trace_id, api_uuid, api_path, api_method, api_version,
    app_uuid, app_name, caller_ip,
    http_status, is_success, is_timeout, latency_ms,
    error_type, error_msg, ts
"""


async def list_calls(
    query: CallQuery,
    *,
    viewer_tenant_id: str | None = None,
    use_admin_session: bool = False,
) -> list[dict[str, Any]]:
    """列表查询 —— 默认按 ts 倒序。"""
    where, params = _build_where(query, viewer_tenant_id=viewer_tenant_id)
    params["limit"] = query.limit
    params["offset"] = query.offset

    sql = f"""
        SELECT {_LIST_COLUMNS.strip()}
        FROM api_call_log
        {where}
        ORDER BY ts DESC
        LIMIT %(limit)s OFFSET %(offset)s
    """  # noqa: S608
    try:
        return ch.query_all(
            sql,
            params,
            force_tenant_id=None if use_admin_session else "sentinel",
        )
    except RuntimeError as e:
        log.warning("trace_list_clickhouse_unavailable", error=str(e))
        return []


# ---------- 详情 ----------


_DETAIL_COLUMNS = """
    trace_id, parent_trace_id, span_id,
    api_uuid, api_path, api_method, api_version, api_mode,
    app_uuid, app_name, caller_ip,
    env, gateway_node,
    req_id, req_size, resp_size,
    http_status, biz_code,
    is_success, is_timeout, latency_ms,
    gateway_latency_ms, backend_latency_ms,
    is_streaming, token_prompt, token_completion, token_total, ai_model,
    error_type, error_msg,
    is_retry, retry_no, task_id, ts
"""


async def get_call(
    trace_id: str,
    *,
    viewer_tenant_id: str | None = None,
    use_admin_session: bool = False,
) -> dict[str, Any]:
    """单条调用详情（含 token / latency / retry）。"""
    params: dict[str, Any] = {"trace_id": trace_id}
    tenant_clause = ""
    if viewer_tenant_id is not None:
        tenant_clause = "AND tenant_id = %(tenant_id)s"
        params["tenant_id"] = int(viewer_tenant_id) if viewer_tenant_id.isdigit() else 0

    sql = f"""
        SELECT {_DETAIL_COLUMNS.strip()}
        FROM api_call_log
        WHERE trace_id = %(trace_id)s {tenant_clause}
        LIMIT 1
    """  # noqa: S608
    try:
        row = ch.query_one(
            sql,
            params,
            force_tenant_id=None if use_admin_session else "sentinel",
        )
    except RuntimeError as e:
        log.warning("trace_detail_clickhouse_unavailable", error=str(e))
        row = None

    if not row:
        raise ApiError(
            ErrorCode.NOT_FOUND,
            f"call {trace_id} not found"
            + (" (or not in your tenant)" if viewer_tenant_id else ""),
        )
    return row


# ---------- 统计 ----------


async def stats(
    query: CallQuery,
    *,
    viewer_tenant_id: str | None = None,
    use_admin_session: bool = False,
) -> dict[str, Any]:
    """聚合统计 —— total / 成功率 / 分位延迟 / top APIs / by hour。

    响应结构对齐 CallStats。
    """
    where, params = _build_where(query, viewer_tenant_id=viewer_tenant_id)

    base_sql = f"""
        SELECT
            count() AS total,
            countIf(is_success = 1) AS success_count,
            countIf(is_success = 0) AS failed_count,
            countIf(is_timeout = 1) AS timeout_count,
            quantile(0.5)(latency_ms) AS p50_latency_ms,
            quantile(0.95)(latency_ms) AS p95_latency_ms,
            quantile(0.99)(latency_ms) AS p99_latency_ms,
            avg(latency_ms) AS avg_latency_ms
        FROM api_call_log
        {where}
    """  # noqa: S608

    # qps = total / 时间窗口秒数（限定窗口）
    window_seconds = 60.0  # 默认 1 分钟（无 since/until 时算最近分钟）
    if query.since and query.until:
        delta = (query.until - query.since).total_seconds()
        if delta > 0:
            window_seconds = delta

    try:
        base = ch.query_one(
            base_sql,
            params,
            force_tenant_id=None if use_admin_session else "sentinel",
        ) or {}
    except RuntimeError as e:
        log.warning("trace_stats_clickhouse_unavailable", error=str(e))
        return _empty_stats()

    total = int(base.get("total", 0))
    success_count = int(base.get("success_count", 0))

    top_apis_sql = f"""
        SELECT
            api_uuid AS api_id,
            api_path,
            count() AS n,
            countIf(is_success = 1) AS success_n
        FROM api_call_log
        {where}
        GROUP BY api_uuid, api_path
        ORDER BY n DESC
        LIMIT 10
    """  # noqa: S608
    try:
        top_apis_raw = ch.query_all(
            top_apis_sql,
            params,
            force_tenant_id=None if use_admin_session else "sentinel",
        )
    except RuntimeError:
        top_apis_raw = []

    top_apis = [
        {
            "api_id": r["api_id"],
            "api_path": r["api_path"],
            "n": int(r["n"]),
            "success_rate": (int(r["success_n"]) / int(r["n"])) if int(r["n"]) else 0.0,
        }
        for r in top_apis_raw
    ]

    by_hour_sql = f"""
        SELECT
            toString(ts_hour) AS hour,
            count() AS n,
            countIf(is_success = 1) AS success_n
        FROM api_call_log
        {where}
        GROUP BY ts_hour
        ORDER BY ts_hour DESC
        LIMIT 168
    """  # noqa: S608
    try:
        by_hour_raw = ch.query_all(
            by_hour_sql,
            params,
            force_tenant_id=None if use_admin_session else "sentinel",
        )
    except RuntimeError:
        by_hour_raw = []

    by_hour = [
        {
            "hour": r["hour"],
            "n": int(r["n"]),
            "success_rate": (int(r["success_n"]) / int(r["n"])) if int(r["n"]) else 0.0,
        }
        for r in by_hour_raw
    ]

    return {
        "total": total,
        "success_count": success_count,
        "failed_count": int(base.get("failed_count", 0)),
        "timeout_count": int(base.get("timeout_count", 0)),
        "success_rate": (success_count / total) if total else 0.0,
        "p50_latency_ms": float(base.get("p50_latency_ms", 0) or 0),
        "p95_latency_ms": float(base.get("p95_latency_ms", 0) or 0),
        "p99_latency_ms": float(base.get("p99_latency_ms", 0) or 0),
        "avg_latency_ms": float(base.get("avg_latency_ms", 0) or 0),
        "qps": (total / window_seconds) if window_seconds > 0 else 0.0,
        "top_apis": top_apis,
        "by_hour": by_hour,
    }


def _empty_stats() -> dict[str, Any]:
    return {
        "total": 0,
        "success_count": 0,
        "failed_count": 0,
        "timeout_count": 0,
        "success_rate": 0.0,
        "p50_latency_ms": 0.0,
        "p95_latency_ms": 0.0,
        "p99_latency_ms": 0.0,
        "avg_latency_ms": 0.0,
        "qps": 0.0,
        "top_apis": [],
        "by_hour": [],
    }
