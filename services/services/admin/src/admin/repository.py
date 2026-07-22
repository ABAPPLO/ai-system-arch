"""audit_log SQL —— 写入用 admin_db_session（绕 RLS，避免审计失败影响业务），
查询用 db_session（租户隔离，超管走 admin_db_session）。

写入策略：best-effort。失败只记 warning，不抛 —— 审计挂了不应该影响业务。
"""

import re
from datetime import datetime
from typing import Any

from apihub_core import db
from apihub_core.config import get_settings
from apihub_core.errors import ApiError, ErrorCode
from apihub_core.logging import get_logger

from admin.models import AuditQuery, AuditRecord

log = get_logger(__name__)


# ---------- 写 ----------


async def record(entry: AuditRecord) -> int:
    """写入一条审计。返回 audit_log.id。

    失败 best-effort：返回 0，不抛。
    """
    try:
        async with db.admin_db_session() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO audit_log (
                    tenant_id, actor_type, actor_id, actor_name, actor_ip,
                    auth_method, action, resource_type, resource_id, resource_name,
                    env, detail, user_agent, request_id, trace_id
                ) VALUES (
                    $1, $2, $3, $4, $5::inet,
                    $6, $7, $8, $9, $10,
                    $11, $12::jsonb, $13, $14, $15
                )
                RETURNING id
                """,
                entry.tenant_id,
                entry.actor_type,
                entry.actor_id,
                entry.actor_name,
                entry.actor_ip,
                entry.auth_method,
                entry.action,
                entry.resource_type,
                entry.resource_id,
                entry.resource_name,
                entry.env,
                entry.detail,
                entry.user_agent,
                entry.request_id,
                entry.trace_id,
            )
        return row["id"] if row else 0
    except Exception as e:
        log.warning("audit_record_failed", error=str(e), action=entry.action)
        return 0


async def record_many(entries: list[AuditRecord]) -> int:
    """批量写。返回成功插入的条数。"""
    if not entries:
        return 0
    success = 0
    async with db.admin_db_session() as conn:
        for entry in entries:
            try:
                await conn.execute(
                    """
                    INSERT INTO audit_log (
                        tenant_id, actor_type, actor_id, actor_name, actor_ip,
                        auth_method, action, resource_type, resource_id, resource_name,
                        env, detail, user_agent, request_id, trace_id
                    ) VALUES (
                        $1, $2, $3, $4, $5::inet,
                        $6, $7, $8, $9, $10,
                        $11, $12::jsonb, $13, $14, $15
                    )
                    """,
                    entry.tenant_id,
                    entry.actor_type,
                    entry.actor_id,
                    entry.actor_name,
                    entry.actor_ip,
                    entry.auth_method,
                    entry.action,
                    entry.resource_type,
                    entry.resource_id,
                    entry.resource_name,
                    entry.env,
                    entry.detail,
                    entry.user_agent,
                    entry.request_id,
                    entry.trace_id,
                )
                success += 1
            except Exception as e:
                log.warning("audit_record_failed", error=str(e))
    return success


# ---------- 读 ----------


def _build_where(query: AuditQuery, *, viewer_tenant_id: str | None) -> tuple[str, list[Any]]:
    """构造 WHERE 子句。

    如果 viewer_tenant_id 给了（普通用户视角），强制 tenant_id = 该值，
    忽略 query.tenant_id（防越权）。
    """
    clauses: list[str] = []
    params: list[Any] = []

    if viewer_tenant_id is not None:
        params.append(viewer_tenant_id)
        clauses.append(f"tenant_id = ${len(params)}")
    elif query.tenant_id is not None:
        params.append(query.tenant_id)
        clauses.append(f"tenant_id = ${len(params)}")

    if query.actor_id is not None:
        params.append(query.actor_id)
        clauses.append(f"actor_id = ${len(params)}")
    if query.action is not None:
        params.append(query.action)
        clauses.append(f"action = ${len(params)}")
    if query.resource_type is not None:
        params.append(query.resource_type)
        clauses.append(f"resource_type = ${len(params)}")
    if query.resource_id is not None:
        params.append(query.resource_id)
        clauses.append(f"resource_id = ${len(params)}")
    if query.since is not None:
        params.append(query.since)
        clauses.append(f"created_at >= ${len(params)}")
    if query.until is not None:
        params.append(query.until)
        clauses.append(f"created_at < ${len(params)}")

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


async def list_events(
    query: AuditQuery,
    *,
    viewer_tenant_id: str | None = None,
    use_admin_session: bool = False,
) -> list[dict[str, Any]]:
    """列表查询。"""
    where, params = _build_where(query, viewer_tenant_id=viewer_tenant_id)
    params.extend([query.limit, query.offset])

    sql = f"""
        SELECT id, tenant_id, actor_type, actor_id, actor_name,
               action, resource_type, resource_id, resource_name, created_at
        FROM audit_log
        {where}
        ORDER BY created_at DESC
        LIMIT ${len(params) - 1} OFFSET ${len(params)}
    """  # noqa: S608 - where is built internally, params are bound

    session = db.admin_db_session if use_admin_session else db.db_session
    async with session() as conn:
        rows = await conn.fetch(sql, *params)
    return [dict(r) for r in rows]


async def get_event(
    audit_id: int,
    *,
    viewer_tenant_id: str | None = None,
    use_admin_session: bool = False,
) -> dict[str, Any]:
    """单条详情。"""
    clauses = ["id = $1"]
    params: list[Any] = [audit_id]
    if viewer_tenant_id is not None:
        params.append(viewer_tenant_id)
        clauses.append(f"tenant_id = ${len(params)}")

    sql = f"""
        SELECT id, tenant_id, actor_type, actor_id, actor_name, actor_ip,
               auth_method, action, resource_type, resource_id, resource_name,
               env, detail, user_agent, request_id, trace_id, created_at
        FROM audit_log
        WHERE {' AND '.join(clauses)}
    """  # noqa: S608

    session = db.admin_db_session if use_admin_session else db.db_session
    async with session() as conn:
        row = await conn.fetchrow(sql, *params)
    if not row:
        raise ApiError(ErrorCode.NOT_FOUND, f"audit event {audit_id} not found")
    out = dict(row)
    # asyncpg inet 类型可能返回 IPv4Address 或 str，统一成 str
    ip = out.get("actor_ip")
    if ip is not None and not isinstance(ip, str):
        out["actor_ip"] = str(ip)
    return out


async def count(
    query: AuditQuery,
    *,
    viewer_tenant_id: str | None = None,
    use_admin_session: bool = False,
) -> int:
    """统计满足条件的总数。"""
    where, params = _build_where(query, viewer_tenant_id=viewer_tenant_id)
    sql = f"SELECT COUNT(*) AS n FROM audit_log {where}"  # noqa: S608

    session = db.admin_db_session if use_admin_session else db.db_session
    async with session() as conn:
        row = await conn.fetchrow(sql, *params)
    return int(row["n"]) if row else 0


async def stats(
    *,
    viewer_tenant_id: str | None = None,
    use_admin_session: bool = False,
    days: int = 7,
) -> dict[str, Any]:
    """聚合统计：top_actions / top_actors / by_day。

    by_day 窗口 = days；top_actions / top_actors 用最近 30 天。
    """
    session = db.admin_db_session if use_admin_session else db.db_session
    tenant_clause = ""
    params: list[Any] = []
    if viewer_tenant_id is not None:
        params.append(viewer_tenant_id)
        tenant_clause = f"WHERE tenant_id = ${len(params)}"

    async with session() as conn:
        # total
        total_row = await conn.fetchrow(
            f"SELECT COUNT(*) AS n FROM audit_log {tenant_clause}",  # noqa: S608
            *params,
        )
        total = int(total_row["n"]) if total_row else 0

        # top actions (30 天)
        p = list(params) + [30]
        top_actions = await conn.fetch(
            f"""
            SELECT action, COUNT(*) AS n
            FROM audit_log
            {tenant_clause + (' AND ' if tenant_clause else 'WHERE ')}
            created_at > NOW() - interval '30 days'
            GROUP BY action
            ORDER BY n DESC
            LIMIT 10
            """,  # noqa: S608
            *p if tenant_clause else p[1:],
        )

        # top actors (30 天)
        top_actors = await conn.fetch(
            f"""
            SELECT actor_id, actor_name, COUNT(*) AS n
            FROM audit_log
            {tenant_clause + (' AND ' if tenant_clause else 'WHERE ')}
            created_at > NOW() - interval '30 days'
              AND actor_id IS NOT NULL
            GROUP BY actor_id, actor_name
            ORDER BY n DESC
            LIMIT 10
            """,  # noqa: S608
            *p if tenant_clause else p[1:],
        )

        # by_day（最近 N 天）
        p2 = list(params) + [days]
        by_day = await conn.fetch(
            f"""
            SELECT DATE(created_at) AS day, COUNT(*) AS n
            FROM audit_log
            {tenant_clause + (' AND ' if tenant_clause else 'WHERE ')}
            created_at > NOW() - interval '{days} days'
            GROUP BY day
            ORDER BY day DESC
            """,  # noqa: S608
            *p2 if tenant_clause else p2[1:],
        )

    return {
        "total": total,
        "top_actions": [dict(r) for r in top_actions],
        "top_actors": [dict(r) for r in top_actors],
        "by_day": [{"day": str(r["day"]), "n": int(r["n"])} for r in by_day],
    }


async def archive_before(cutoff: datetime) -> int:
    """清理：把早于 cutoff 的归档到 OSS 并删除。

    逐批读取（每批 1000 条），按 tenant+月份分组，每组写一个 JSONL gzip
    文件上传到 audit-archive bucket。成功上传后删除 PG 中的已归档记录。

    返回归档条数。
    """
    import gzip
    import json as stdjson

    from apihub_core import oss

    BATCH = 1000
    total = 0

    while True:
        batch_start = datetime.now()
        async with db.admin_db_session() as conn:
            rows = await conn.fetch(
                "SELECT id FROM audit_log WHERE created_at < $1" " ORDER BY id LIMIT $2",
                cutoff,
                BATCH,
            )
            ids = [r["id"] for r in rows]
            if not ids:
                break

            records = await conn.fetch(
                "SELECT * FROM audit_log WHERE id = ANY($1::bigint[])",
                ids,
            )

        # 按 (tenant_id, yyyy, mm) 分组
        groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for r in records:
            rec = dict(r)
            ts = rec.get("created_at") or cutoff
            ym = ts.strftime("%Y"), ts.strftime("%m")
            key = (rec["tenant_id"], *ym)
            groups.setdefault(key, []).append(rec)

        ok_ids: list[int] = []
        settings = get_settings()
        for (tenant_id, yyyy, mm), recs in groups.items():
            lines = "\n".join(stdjson.dumps(r, default=str) for r in recs).encode("utf-8")
            compressed = gzip.compress(lines)
            oss_key = f"{yyyy}/{mm}/tenant-{tenant_id}-{yyyy}-{mm}.jsonl.gz"
            uploaded = await oss.put_object(
                settings.oss_bucket_audit,
                oss_key,
                compressed,
            )
            if uploaded:
                ok_ids.extend(r["id"] for r in recs)
            else:
                log.warning(
                    "audit_archive_upload_skipped",
                    tenant_id=tenant_id,
                    key=oss_key,
                    count=len(recs),
                )

        if ok_ids:
            async with db.admin_db_session() as conn:
                await conn.execute(
                    "DELETE FROM audit_log WHERE id = ANY($1::bigint[])",
                    ok_ids,
                )

            n = len(ok_ids)
            total += n
            elapsed = (datetime.now() - batch_start).total_seconds()
            log.info(
                "audit_archive_batch_done",
                batch_size=n,
                running_total=total,
                elapsed_seconds=round(elapsed, 2),
            )

        if len(ids) < BATCH:
            break

    log.info("audit_archive_done", total=total, cutoff=cutoff.isoformat())
    return total


async def cleanup_task_partitions(*, before: datetime) -> int:
    """删除早于 before 的 task_instance 分区。

    分区命名格式：task_instance_YYYY_MM。判断条件：分区结束日期 < before。
    返回删除的分区数。
    """
    dropped = 0
    async with db.admin_db_session() as conn:
        rows = await conn.fetch(
            "SELECT relname AS partition_name,"
            " pg_get_expr(relpartbound, reltoastrelid) AS bound_expr"
            " FROM pg_class WHERE relispartition = true"
            " AND relname LIKE 'task_instance_%'"
            " ORDER BY relname",
        )
        for r in rows:
            # bound_expr 形如 FOR VALUES FROM ('2026-07-01') TO ('2026-08-01')
            bound = r["bound_expr"]
            m = re.search(r"TO\s+\('(\d{4}-\d{2}-\d{2})'\)", bound)
            if m and datetime.fromisoformat(m.group(1)) < before:
                await conn.execute(f"DROP TABLE IF EXISTS {r['partition_name']} CASCADE")
                dropped += 1
                log.info("cleanup_dropped_partition", partition=r["partition_name"])
    return dropped


async def cleanup_retry_tasks(*, before: datetime) -> int:
    """删除早于 before 的已结束重试任务（terminal status 且超过保留期）。"""
    async with db.admin_db_session() as conn:
        result = await conn.execute(
            "DELETE FROM retry_task"
            " WHERE status NOT IN ('pending', 'running')"
            " AND created_at < $1",
            before,
        )
    n = _parse_delete_count(result)
    log.info("cleanup_retry_tasks_done", deleted=n, before=before.isoformat())
    return n


def _parse_delete_count(result: str) -> int:
    """解析 PostgreSQL DELETE 返回 'DELETE N' → int。"""
    try:
        return int(result.split()[-1])
    except (ValueError, IndexError):
        return 0
