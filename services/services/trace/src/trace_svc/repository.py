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
        params["tenant_id"] = viewer_tenant_id  # String，原样透传

    if query.api_id:
        clauses.append("api_id = %(api_id)s")
        params["api_id"] = query.api_id
    if query.app_id:
        clauses.append("app_id = %(app_id)s")
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
        # 精简 schema 无 is_timeout 列 → 按 error_code 近似
        clauses.append("error_code LIKE %(timeout_pat)s")
        params["timeout_pat"] = "%timeout%"

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


# ---------- 列表 ----------


_LIST_COLUMNS = """
    trace_id, api_id, path, method, api_version_id,
    app_id, client_ip,
    status_code, is_success, latency_ms,
    error_code, error_msg, ts
"""


# 跨区拼接后，admin 侧需要全局 merge：list_calls 按 ts DESC 合并后再全局切片；
# stats 的 base/top_apis/by_hour 也各自 merge（见下）。单 Region 路径不走这些 helper。

_LIST_PER_REGION_CAP = 1000  # 跨区查询时每 Region 的行数上限，全局 offset/limit 在 merge 后切片


async def list_calls(
    query: CallQuery,
    *,
    viewer_tenant_id: str | None = None,
    use_admin_session: bool = False,
) -> list[dict[str, Any]]:
    """列表查询 —— 默认按 ts 倒序。

    admin 跨区路径：每 Region 跑 `LIMIT %(cap)s`（无 OFFSET），拼接后按 ts DESC
    全局 merge-sort，再对全局 [offset:offset+limit] 切片——保证翻页是全局语义。
    单 Region 普通路径仍走 query_all，SQL 内 LIMIT/OFFSET 不变。
    """
    where, params = _build_where(query, viewer_tenant_id=viewer_tenant_id)
    params["cap"] = _LIST_PER_REGION_CAP

    sql = f"""
        SELECT {_LIST_COLUMNS.strip()}
        FROM api_call_log
        {where}
        ORDER BY ts DESC
        LIMIT %(cap)s
    """  # noqa: S608
    try:
        if use_admin_session:
            rows = ch.query_union_peer(sql, sql, params, force_tenant_id=None)
            # 跨区拼接后按 ts DESC 全局排序，再切片全局 offset/limit
            rows.sort(key=lambda r: r.get("ts"), reverse=True)
            return rows[query.offset : query.offset + query.limit]
        # 单 Region：SQL LIMIT/OFFSET 语义即全局，直接用
        params["limit"] = query.limit
        params["offset"] = query.offset
        local_sql = f"""
            SELECT {_LIST_COLUMNS.strip()}
            FROM api_call_log
            {where}
            ORDER BY ts DESC
            LIMIT %(limit)s OFFSET %(offset)s
        """  # noqa: S608
        return ch.query_all(local_sql, params, force_tenant_id="sentinel")
    except RuntimeError as e:
        log.warning("trace_list_clickhouse_unavailable", error=str(e))
        return []


# ---------- 详情 ----------


_DETAIL_COLUMNS = """
    trace_id, api_id, path, method, api_version_id,
    app_id, client_ip,
    request_id, request_size, response_size,
    status_code, is_success, latency_ms, backend_latency_ms,
    ai_streaming, token_prompt, token_completion, token_total, ai_model,
    error_code, error_msg, ts
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
        params["tenant_id"] = viewer_tenant_id  # String，原样透传

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
            f"call {trace_id} not found" + (" (or not in your tenant)" if viewer_tenant_id else ""),
        )
    return row


# ---------- 跨区 merge helpers (admin 路径专用) ----------


_BASE_COUNT_FIELDS = ("total", "success_count", "failed_count", "timeout_count")
_BASE_QUANTILE_FIELDS = (
    "p50_latency_ms",
    "p95_latency_ms",
    "p99_latency_ms",
    "avg_latency_ms",
)


def _merge_base_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge base_sql rows across regions.

    counts (total/success/failed/timeout) are SUM'd across regions;
    p50/p95/p99/avg_latency are taken from the FIRST (local) row as a proxy —
    true cross-region quantiles need SQL remote() UNION (deferred per spec §8-R4).
    """
    if not rows:
        return {}
    merged: dict[str, Any] = {}
    for f in _BASE_COUNT_FIELDS:
        merged[f] = sum(int(r.get(f, 0) or 0) for r in rows)
    for f in _BASE_QUANTILE_FIELDS:
        merged[f] = rows[0].get(f, 0)
    return merged


def _merge_top_apis(rows: list[dict[str, Any]], *, limit: int = 10) -> list[dict[str, Any]]:
    """跨区 top_apis：按 (api_id, path) 合并 n/success_n 求和，再按 n DESC 排序，截 limit。"""
    by_key: dict[tuple, dict[str, Any]] = {}
    for r in rows:
        key = (r.get("api_id"), r.get("path"))
        agg = by_key.get(key)
        if agg is None:
            agg = {
                "api_id": r.get("api_id"),
                "path": r.get("path"),
                "n": 0,
                "success_n": 0,
            }
            by_key[key] = agg
        agg["n"] += int(r.get("n", 0) or 0)
        agg["success_n"] += int(r.get("success_n", 0) or 0)
    merged = list(by_key.values())
    merged.sort(key=lambda x: int(x.get("n", 0) or 0), reverse=True)
    return merged[:limit]


def _merge_by_hour(rows: list[dict[str, Any]], *, limit: int = 168) -> list[dict[str, Any]]:
    """跨区 by_hour：按 hour 合并 n/success_n 求和，再按 hour DESC 排序（对齐 SQL 原序），截 limit。"""
    by_key: dict[str, dict[str, Any]] = {}
    for r in rows:
        h = r.get("hour")
        agg = by_key.get(h)
        if agg is None:
            agg = {"hour": h, "n": 0, "success_n": 0}
            by_key[h] = agg
        agg["n"] += int(r.get("n", 0) or 0)
        agg["success_n"] += int(r.get("success_n", 0) or 0)
    merged = list(by_key.values())
    merged.sort(key=lambda x: x.get("hour") or "", reverse=True)
    return merged[:limit]


# ---------- 统计 ----------


async def stats(
    query: CallQuery,
    *,
    viewer_tenant_id: str | None = None,
    use_admin_session: bool = False,
) -> dict[str, Any]:
    """聚合统计 —— total / 成功率 / 分位延迟 / top APIs / by hour。

    响应结构对齐 CallStats。admin 路径走 query_union_peer 后 merge：
      - base: counts 跨区求和、quantiles 取本地行（proxy）
      - top_apis / by_hour: 按 key 合并求和后重排
    """
    where, params = _build_where(query, viewer_tenant_id=viewer_tenant_id)

    base_sql = f"""
        SELECT
            count() AS total,
            countIf(is_success = 1) AS success_count,
            countIf(is_success = 0) AS failed_count,
            countIf(error_code LIKE '%timeout%') AS timeout_count,
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
        if use_admin_session:
            base = _merge_base_rows(
                ch.query_union_peer(base_sql, base_sql, params, force_tenant_id=None)
            )
        else:
            base = ch.query_one(base_sql, params, force_tenant_id="sentinel") or {}
    except RuntimeError as e:
        log.warning("trace_stats_clickhouse_unavailable", error=str(e))
        return _empty_stats()

    total = int(base.get("total", 0))
    success_count = int(base.get("success_count", 0))

    top_apis_sql = f"""
        SELECT
            api_id,
            path,
            count() AS n,
            countIf(is_success = 1) AS success_n
        FROM api_call_log
        {where}
        GROUP BY api_id, path
        ORDER BY n DESC
        LIMIT 10
    """  # noqa: S608
    try:
        if use_admin_session:
            top_apis_raw = _merge_top_apis(
                ch.query_union_peer(top_apis_sql, top_apis_sql, params, force_tenant_id=None),
                limit=10,
            )
        else:
            top_apis_raw = ch.query_all(top_apis_sql, params, force_tenant_id="sentinel")
    except RuntimeError:
        top_apis_raw = []

    top_apis = [
        {
            "api_id": r["api_id"],
            "api_path": r["path"],
            "n": int(r["n"]),
            "success_rate": (int(r["success_n"]) / int(r["n"])) if int(r["n"]) else 0.0,
        }
        for r in top_apis_raw
    ]

    by_hour_sql = f"""
        SELECT
            toString(toStartOfHour(ts)) AS hour,
            count() AS n,
            countIf(is_success = 1) AS success_n
        FROM api_call_log
        {where}
        GROUP BY toStartOfHour(ts)
        ORDER BY toStartOfHour(ts) DESC
        LIMIT 168
    """  # noqa: S608
    try:
        if use_admin_session:
            by_hour_raw = _merge_by_hour(
                ch.query_union_peer(by_hour_sql, by_hour_sql, params, force_tenant_id=None),
                limit=168,
            )
        else:
            by_hour_raw = ch.query_all(by_hour_sql, params, force_tenant_id="sentinel")
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


# ---------- 高级分析 ----------


async def call_funnel(
    *,
    viewer_tenant_id: str | None = None,
    use_admin_session: bool = False,
    since: str = "",
    until: str = "",
    limit: int = 50,
) -> list[dict[str, Any]]:
    """调用漏斗：按 trace_id 分组，展示每次调用链中的 API 序列。"""
    params: dict[str, Any] = {}
    clauses: list[str] = []
    if viewer_tenant_id:
        clauses.append("tenant_id = %(tenant_id)s")
        params["tenant_id"] = viewer_tenant_id
    if since:
        clauses.append("ts >= %(since)s")
        params["since"] = since
    if until:
        clauses.append("ts < %(until)s")
        params["until"] = until
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    try:
        sql = f"""
            SELECT trace_id, groupArray((ts, api_id, path)) AS steps
            FROM api_call_log {where}
            GROUP BY trace_id ORDER BY max(ts) DESC LIMIT {limit}
            """  # noqa: S608
        if use_admin_session:
            rows = ch.query_union_peer(sql, sql, params, force_tenant_id=None)
        else:
            rows = ch.query_all(sql, params, force_tenant_id="sentinel")
    except RuntimeError as e:
        log.warning("funnel_ch_unavailable", error=str(e))
        return []
    result = []
    for r in rows:
        steps = (r.get("steps") or [])[:20]
        result.append(
            {
                "trace_id": r["trace_id"],
                "step_count": len(steps),
                "steps": [{"api_id": s[1], "path": s[2]} for s in steps],
            }
        )
    return result


async def co_occurrence(
    *,
    viewer_tenant_id: str | None = None,
    use_admin_session: bool = False,
    since: str = "",
    min_pairs: int = 3,
) -> list[dict[str, Any]]:
    """API 共现：同一 trace 中的 API 对，按频次降序。"""
    params: dict[str, Any] = {}
    clauses: list[str] = []
    if viewer_tenant_id:
        clauses.append("tenant_id = %(tenant_id)s")
        params["tenant_id"] = viewer_tenant_id
    if since:
        clauses.append("ts >= %(since)s")
        params["since"] = since
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    try:
        sql = f"""
            SELECT a.api_id AS api_a, a.path AS path_a,
                   b.api_id AS api_b, b.path AS path_b,
                   count() AS pair_count
            FROM api_call_log a
            JOIN api_call_log b ON a.trace_id = b.trace_id AND a.api_id < b.api_id
            {where}
            GROUP BY a.api_id, a.path, b.api_id, b.path
            HAVING pair_count >= %(min)s
            ORDER BY pair_count DESC LIMIT 30
            """  # noqa: S608
        if use_admin_session:
            rows = ch.query_union_peer(sql, sql, {**params, "min": min_pairs}, force_tenant_id=None)
        else:
            rows = ch.query_all(sql, {**params, "min": min_pairs}, force_tenant_id="sentinel")
    except RuntimeError as e:
        log.warning("cooccur_ch_unavailable", error=str(e))
        return []
    return [
        {
            "api_a": r["api_a"],
            "path_a": r["path_a"],
            "api_b": r["api_b"],
            "path_b": r["path_b"],
            "pair_count": int(r["pair_count"]),
        }
        for r in rows
    ]


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
