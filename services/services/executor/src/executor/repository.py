"""task 表的 PG 状态机操作。

executor 是后台 worker，没有入站 HTTP → 没有自动的 TenantContext。
所有操作走 admin_db_session，用 task.tenant_id 显式过滤（多一行 WHERE
兜底，防止 RLS 配置错漏）。
"""

import contextlib

from apihub_core import db


async def get_task_status(task_id: str) -> str | None:
    """读当前状态 —— 用于幂等检查（at-least-once 可能重投）。"""
    async with db.admin_db_session() as conn:
        row = await conn.fetchrow(
            "SELECT status FROM task WHERE id = $1",
            task_id,
        )
    return row["status"] if row else None


async def mark_running(task_id: str) -> bool:
    """pending → running。返回是否真的转换了（False = 已被其他 worker 抢走 / 状态不对）。"""
    async with db.admin_db_session() as conn:
        result = await conn.execute(
            """
            UPDATE task
            SET status = 'running', started_at = NOW(), updated_at = NOW()
            WHERE id = $1 AND status = 'pending'
            """,
            task_id,
        )
    # asyncpg 的 result 是 "UPDATE 1" / "UPDATE 0"
    return result.endswith(" 1")


async def mark_succeeded(
    task_id: str,
    response_body: str,
    http_status: int,
) -> None:
    async with db.admin_db_session() as conn:
        await conn.execute(
            """
            UPDATE task
            SET status = 'succeeded',
                response_body = $2,
                response_status = $3,
                finished_at = NOW(),
                updated_at = NOW()
            WHERE id = $1
            """,
            task_id,
            response_body,
            http_status,
        )


async def mark_failed(
    task_id: str,
    error_code: str,
    error_msg: str,
    http_status: int | None = None,
) -> None:
    """失败 / 超时 / 不可达 —— 都走这个。status 由调用方决定。"""
    async with db.admin_db_session() as conn:
        await conn.execute(
            """
            UPDATE task
            SET status = CASE
                  WHEN $2 = 'timeout' THEN 'timeout'
                  ELSE 'failed'
                END,
                error_code = $2,
                error_msg = $3,
                response_status = $4,
                finished_at = NOW(),
                updated_at = NOW()
            WHERE id = $1
            """,
            task_id,
            error_code,
            error_msg,
            http_status,
        )


async def reset_stale_running(timeout_seconds: int = 600) -> int:
    """启动时清理：把上次 worker 崩掉留下的 running 任务重置为 pending。

    Returns: 重置的行数。
    """
    async with db.admin_db_session() as conn:
        with contextlib.suppress(Exception):
            result = await conn.execute(
                """
                UPDATE task
                SET status = 'pending', started_at = NULL, updated_at = NOW()
                WHERE status = 'running'
                  AND started_at < NOW() - ($1 || ' seconds')::interval
                """,
                str(timeout_seconds),
            )
    # "UPDATE N"
    try:
        return int(result.split()[-1])
    except (IndexError, ValueError):
        return 0
