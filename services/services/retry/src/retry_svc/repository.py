"""retry_task / retry_attempt 表的 PG 操作。

和 executor 一样是后台 worker，没有入站 HTTP → 没 TenantContext。
所有写操作走 admin_db_session + 显式 tenant_id（RLS 兜底）。
读操作（HTTP route 上下文）走 db_session（自动 SET LOCAL app.tenant_id）。
"""

from datetime import UTC, datetime

import asyncpg
from apihub_core import db

from retry_svc.models import (
    BackoffPolicy,
    ListFailedQuery,
    RetryAttemptRow,
    RetryStatus,
    RetryTaskDetail,
    RetryTaskRow,
)

# ============ 写操作（admin pool，显式 tenant_id）============


async def create_retry_task(
    *,
    tenant_id: str,
    trace_id: str,
    api_id: str,
    app_id: str,
    task_instance_id: str | None,
    original_request: dict,
    error_code: str,
    error_msg: str,
    max_attempts: int,
    backoff_policy: BackoffPolicy,
    backoff_base_ms: int,
    next_retry_at: datetime,
    env: str,
) -> int:
    """INSERT 新 retry_task。返回新 id（>0）；返回 0 表示被去重跳过。

    幂等靠 partial unique index idx_retry_task_active_dedup
    (UNIQUE(task_instance_id) WHERE status IN ('pending','running'))：
    Kafka at-least-once 重投同一 TaskFailure 时，ON CONFLICT DO NOTHING 命中该索引
    → INSERT 静默跳过 → fetchrow 返回 None → 这里返回 0，调用方据此跳过入队。
    上次重试已 dead/succeeded/ignored 后再次失败可正常建新行（不在 active 集合内）。
    """
    async with db.admin_db_session() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO retry_task (
                tenant_id, trace_id, task_instance_id, api_id, app_id,
                original_request, last_error_code, last_error_msg,
                last_failed_at, max_attempts, retry_count,
                next_retry_at, backoff_policy, backoff_base_ms,
                status, env
            ) VALUES (
                $1, $2, $3, $4, $5,
                $6, $7, $8,
                NOW(), $9, 0,
                $10, $11, $12,
                'pending', $13
            )
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            tenant_id,
            trace_id,
            task_instance_id,
            api_id,
            app_id,
            original_request,
            error_code,
            (error_msg or "")[:5000],
            max_attempts,
            next_retry_at,
            backoff_policy.value,
            backoff_base_ms,
            env,
        )
    return int(row["id"]) if row else 0  # 0 = partial unique 命中去重


async def mark_attempt_started(retry_task_id: int) -> bool:
    """pending → running，原子转换。False = 已被其他 worker 抢 / 状态不对。"""
    async with db.admin_db_session() as conn:
        result = await conn.execute(
            """
            UPDATE retry_task
            SET status = 'running', updated_at = NOW()
            WHERE id = $1 AND status = 'pending'
            """,
            retry_task_id,
        )
    return result.endswith(" 1")


async def mark_succeeded(
    retry_task_id: int,
    *,
    response_status: int,
    response_body: dict | None,
    latency_ms: int,
) -> None:
    async with db.admin_db_session() as conn:
        await conn.execute(
            """
            UPDATE retry_task
            SET status = 'succeeded', updated_at = NOW()
            WHERE id = $1
            """,
            retry_task_id,
        )
        await _insert_attempt(
            conn,
            retry_task_id=retry_task_id,
            tenant_id=await _tenant_of(conn, retry_task_id),
            attempt_no=await _next_attempt_no(conn, retry_task_id),
            response_status=response_status,
            response_body=response_body,
            latency_ms=latency_ms,
        )


async def mark_failed_attempt(
    retry_task_id: int,
    *,
    error_code: str,
    error_msg: str,
    response_status: int | None,
    response_body: dict | None,
    latency_ms: int,
    next_retry_at: datetime | None,
) -> None:
    """记录失败 attempt + 更新 retry_task。

    如果 next_retry_at 为 None，说明超过 max_attempts → status='dead'。
    """
    async with db.admin_db_session() as conn:
        tenant_id = await _tenant_of(conn, retry_task_id)
        attempt_no = await _next_attempt_no(conn, retry_task_id)

        if next_retry_at is None:
            # 进死信
            await conn.execute(
                """
                UPDATE retry_task
                SET status = 'dead',
                    retry_count = $2,
                    last_error_code = $3,
                    last_error_msg = $4,
                    last_failed_at = NOW(),
                    next_retry_at = NULL,
                    updated_at = NOW()
                WHERE id = $1
                """,
                retry_task_id,
                attempt_no,
                error_code,
                (error_msg or "")[:5000],
            )
        else:
            await conn.execute(
                """
                UPDATE retry_task
                SET status = 'pending',
                    retry_count = $2,
                    last_error_code = $3,
                    last_error_msg = $4,
                    last_failed_at = NOW(),
                    next_retry_at = $5,
                    updated_at = NOW()
                WHERE id = $1
                """,
                retry_task_id,
                attempt_no,
                error_code,
                (error_msg or "")[:5000],
                next_retry_at,
            )

        await _insert_attempt(
            conn,
            retry_task_id=retry_task_id,
            tenant_id=tenant_id,
            attempt_no=attempt_no,
            response_status=response_status,
            response_body=response_body,
            error_code=error_code,
            error_msg=(error_msg or "")[:5000],
            latency_ms=latency_ms,
        )


async def mark_ignored(retry_task_id: int) -> bool:
    """手动忽略 → 状态变 ignored。False = 行不存在 / 非 pending/dead 状态。"""
    async with db.admin_db_session() as conn:
        result = await conn.execute(
            """
            UPDATE retry_task
            SET status = 'ignored', updated_at = NOW()
            WHERE id = $1 AND status IN ('pending', 'dead')
            """,
            retry_task_id,
        )
    return result.endswith(" 1")


async def requeue_for_retry(retry_task_id: int) -> tuple[bool, str]:
    """手动 trigger：把 dead / ignored → pending，retry_count 不重置但 next_retry_at=NOW()。

    返回 (success, tenant_id)。False = 行不存在 / 状态不对。
    """
    async with db.admin_db_session() as conn:
        tenant_id = await conn.fetchval(
            "SELECT tenant_id FROM retry_task WHERE id = $1",
            retry_task_id,
        )
        if tenant_id is None:
            return False, ""

        result = await conn.execute(
            """
            UPDATE retry_task
            SET status = 'pending', next_retry_at = NOW(), updated_at = NOW()
            WHERE id = $1 AND status IN ('dead', 'ignored', 'pending')
            """,
            retry_task_id,
        )
    return result.endswith(" 1"), tenant_id


# ============ 读操作（HTTP route 上下文用 db_session，自动 RLS）============


async def get_retry_task(retry_task_id: int) -> RetryTaskDetail | None:
    async with db.db_session() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, tenant_id, trace_id, task_instance_id::text,
                   api_id, app_id, original_request,
                   last_error_code, last_error_msg, last_failed_at,
                   max_attempts, retry_count, next_retry_at,
                   backoff_policy, backoff_base_ms,
                   status, env, created_at, updated_at
            FROM retry_task
            WHERE id = $1
            """,
            retry_task_id,
        )
        if not row:
            return None

        attempts = await conn.fetch(
            """
            SELECT id, tenant_id, retry_task_id, attempt_no,
                   request_body, response_status,
                   response_body, error_code, error_msg,
                   latency_ms, attempted_at
            FROM retry_attempt
            WHERE retry_task_id = $1
            ORDER BY attempt_no ASC
            """,
            retry_task_id,
        )

    base = _row_to_task(row)
    base_dict = base.model_dump()
    base_dict["attempts"] = [_row_to_attempt(a) for a in attempts]
    base_dict["original_request"] = row["original_request"] if row["original_request"] else {}
    return RetryTaskDetail(**base_dict)


async def list_failed(query: ListFailedQuery) -> list[RetryTaskRow]:
    """列出待重试 / 死信 / 忽略的任务。"""
    clauses = ["status = ANY($1::text[])"]
    params: list = [
        [
            RetryStatus.PENDING.value,
            RetryStatus.DEAD.value,
            RetryStatus.IGNORED.value,
        ]
    ]
    idx = 2

    if query.since:
        clauses.append(f"created_at >= ${idx}")
        params.append(query.since)
        idx += 1
    if query.until:
        clauses.append(f"created_at <= ${idx}")
        params.append(query.until)
        idx += 1
    if query.api_id:
        clauses.append(f"api_id = ${idx}")
        params.append(query.api_id)
        idx += 1
    if query.app_id:
        clauses.append(f"app_id = ${idx}")
        params.append(query.app_id)
        idx += 1
    if query.status:
        # 覆盖默认 status 列表，精确查某个状态
        clauses[0] = f"status = ${idx}"
        params[0] = query.status.value
        # 重新对齐：因为 $1 已被替换，剩下的偏移要调整
        # 简化做法：重排
    # 重排参数（status 单独覆盖时，$1 不再是数组）
    if query.status:
        # 重新构造 SQL，避免 $1 数组 vs 字符串冲突
        return await _list_failed_status_single(query)

    sql = f"""
        SELECT id, tenant_id, trace_id, task_instance_id::text,
               api_id, app_id, original_request,
               last_error_code, last_error_msg, last_failed_at,
               max_attempts, retry_count, next_retry_at,
               backoff_policy, backoff_base_ms,
               status, env, created_at, updated_at
        FROM retry_task
        WHERE {" AND ".join(clauses)}
        ORDER BY last_failed_at DESC NULLS LAST, id DESC
        LIMIT ${idx} OFFSET ${idx + 1}
    """  # noqa: S608
    params.extend([query.limit, query.offset])

    async with db.db_session() as conn:
        rows = await conn.fetch(sql, *params)
    return [_row_to_task(r) for r in rows]


async def _list_failed_status_single(query: ListFailedQuery) -> list[RetryTaskRow]:
    """精确查某个状态（覆盖 list_failed 的逻辑）。"""
    clauses = ["status = $1"]
    params: list = [query.status.value]  # type: ignore
    idx = 2
    if query.since:
        clauses.append(f"created_at >= ${idx}")
        params.append(query.since)
        idx += 1
    if query.until:
        clauses.append(f"created_at <= ${idx}")
        params.append(query.until)
        idx += 1
    if query.api_id:
        clauses.append(f"api_id = ${idx}")
        params.append(query.api_id)
        idx += 1
    if query.app_id:
        clauses.append(f"app_id = ${idx}")
        params.append(query.app_id)
        idx += 1

    sql = f"""
        SELECT id, tenant_id, trace_id, task_instance_id::text,
               api_id, app_id, original_request,
               last_error_code, last_error_msg, last_failed_at,
               max_attempts, retry_count, next_retry_at,
               backoff_policy, backoff_base_ms,
               status, env, created_at, updated_at
        FROM retry_task
        WHERE {" AND ".join(clauses)}
        ORDER BY last_failed_at DESC NULLS LAST, id DESC
        LIMIT ${idx} OFFSET ${idx + 1}
    """  # noqa: S608
    params.extend([query.limit, query.offset])

    async with db.db_session() as conn:
        rows = await conn.fetch(sql, *params)
    return [_row_to_task(r) for r in rows]


async def stats() -> dict:
    async with db.db_session() as conn:
        rows = await conn.fetch(
            """
            SELECT status, COUNT(*) AS n
            FROM retry_task
            GROUP BY status
            """
        )
        err_rows = await conn.fetch(
            """
            SELECT COALESCE(last_error_code, 'unknown') AS code, COUNT(*) AS n
            FROM retry_task
            WHERE status IN ('pending', 'dead')
            GROUP BY last_error_code
            ORDER BY n DESC
            LIMIT 10
            """
        )

    counts = {r["status"]: int(r["n"]) for r in rows}
    total = sum(counts.values())
    succeeded = counts.get("succeeded", 0)
    by_error = {r["code"]: int(r["n"]) for r in err_rows}

    return {
        "total": total,
        "pending": counts.get("pending", 0),
        "running": counts.get("running", 0),
        "dead": counts.get("dead", 0),
        "ignored": counts.get("ignored", 0),
        "succeeded": succeeded,
        "success_rate": round(succeeded / total, 4) if total else 0.0,
        "by_error_code": by_error,
    }


# ============ 辅助函数 ============


async def _tenant_of(conn: asyncpg.Connection, retry_task_id: int) -> str:
    return await conn.fetchval(
        "SELECT tenant_id FROM retry_task WHERE id = $1",
        retry_task_id,
    )


async def _next_attempt_no(conn: asyncpg.Connection, retry_task_id: int) -> int:
    """查当前 retry_count + 1 作为下一次 attempt_no。"""
    cur = await conn.fetchval(
        "SELECT retry_count FROM retry_task WHERE id = $1",
        retry_task_id,
    )
    return (cur or 0) + 1


async def _insert_attempt(
    conn: asyncpg.Connection,
    *,
    retry_task_id: int,
    tenant_id: str,
    attempt_no: int,
    request_body: dict | None = None,
    response_status: int | None = None,
    response_body: dict | None = None,
    error_code: str | None = None,
    error_msg: str | None = None,
    latency_ms: int | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO retry_attempt (
            tenant_id, retry_task_id, attempt_no,
            request_body, response_status, response_body,
            error_code, error_msg, latency_ms
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        """,
        tenant_id,
        retry_task_id,
        attempt_no,
        request_body,
        response_status,
        response_body,
        error_code,
        error_msg,
        latency_ms,
    )


def _row_to_task(row: asyncpg.Record) -> RetryTaskRow:
    return RetryTaskRow(
        id=int(row["id"]),
        tenant_id=row["tenant_id"],
        trace_id=row["trace_id"],
        task_instance_id=row["task_instance_id"],
        api_id=row["api_id"],
        app_id=row["app_id"],
        max_attempts=int(row["max_attempts"]),
        retry_count=int(row["retry_count"]),
        next_retry_at=row["next_retry_at"],
        backoff_policy=BackoffPolicy(row["backoff_policy"]),
        backoff_base_ms=int(row["backoff_base_ms"]),
        status=RetryStatus(row["status"]),
        env=row["env"],
        last_error_code=row["last_error_code"],
        last_error_msg=row["last_error_msg"],
        last_failed_at=row["last_failed_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_attempt(row: asyncpg.Record) -> RetryAttemptRow:
    req = row["request_body"]
    resp = row["response_body"]
    return RetryAttemptRow(
        id=int(row["id"]),
        tenant_id=row["tenant_id"],
        retry_task_id=int(row["retry_task_id"]),
        attempt_no=int(row["attempt_no"]),
        request_body=req,
        response_status=row["response_status"],
        response_body=resp,
        error_code=row["error_code"],
        error_msg=row["error_msg"],
        latency_ms=row["latency_ms"],
        attempted_at=row["attempted_at"],
    )


def now_utc() -> datetime:
    """统一时区 aware datetime（写 PG 用）。"""
    return datetime.now(UTC).replace(tzinfo=None)
