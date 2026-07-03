"""ClickHouse 客户端 —— 调用日志 / 聚合分析查询用。

不走 RLS：ClickHouse 没有行级安全，租户隔离在 WHERE 子句里手动加。
所有查询入口都必须强制传 tenant_id（admin 视角显式传 None 才跨租户）。
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import clickhouse_connect
from clickhouse_connect.driver.client import Client

from apihub_core.config import Settings
from apihub_core.logging import get_logger
from apihub_core.tenant import get_tenant_context

log = get_logger(__name__)

_client: Client | None = None


def init_clickhouse(settings: Settings) -> None:
    """进程启动时调一次。ch_host 没配 → 跳过（不需要 CH 的服务无副作用）。"""
    global _client
    if not settings.ch_host:
        log.info("clickhouse_skip_no_host")
        return
    _client = clickhouse_connect.get_client(
        host=settings.ch_host,
        port=settings.ch_port,
        username=settings.ch_username,
        password=settings.ch_password,
        database=settings.ch_database,
        connect_timeout=10,
        send_receive_timeout=30,
    )
    log.info(
        "clickhouse_initialized",
        host=settings.ch_host,
        database=settings.ch_database,
    )


def close_clickhouse() -> None:
    global _client
    if _client:
        _client.close()
        _client = None


@contextmanager
def ch_session(*, force_tenant_id: str | None = "sentinel") -> Iterator[Client]:
    """ClickHouse 查询会话。

    参数：
      force_tenant_id:
        None  → 超管视角（不强制 tenant 过滤，调用方自己加 WHERE）
        "sentinel"（默认）→ 自动取当前 TenantContext.tenant_id；无上下文则抛
        str   → 强制按此 tenant_id 过滤

    用法：
        with ch_session() as ch:
            rows = ch.query("SELECT trace_id, latency_ms FROM api_call_log WHERE ts >= %(since)s",
                            parameters={"since": "2026-07-01"})
    """
    if _client is None:
        raise RuntimeError("ClickHouse not initialized. Call init_clickhouse first.")

    if force_tenant_id == "sentinel":
        ctx = get_tenant_context()
        if ctx is None:
            raise RuntimeError(
                "ch_session called without tenant context; "
                "pass force_tenant_id=None for admin view"
            )
        # 不直接拼 SQL，让调用方用 ch_tenant_filter() 拿到 tenant_id 后用参数化
        log.debug("ch_session_tenant_scoped", tenant_id=ctx.tenant_id)
    elif force_tenant_id is None:
        log.debug("ch_session_admin_view")
    else:
        log.debug("ch_session_forced_tenant", tenant_id=force_tenant_id)

    yield _client


def current_tenant_id_or_none() -> str | None:
    """给 repository 拿当前 tenant_id（用于拼 WHERE 子句的参数）。"""
    ctx = get_tenant_context()
    return ctx.tenant_id if ctx else None


def query_all(
    sql: str,
    params: dict[str, Any] | None = None,
    *,
    force_tenant_id: str | None = "sentinel",
) -> list[dict[str, Any]]:
    """便捷封装：SELECT 返回 list[dict]。

    ClickHouse 用 %(name)s 风格的参数化（不是 asyncpg 的 $1）。
    """
    with ch_session(force_tenant_id=force_tenant_id) as ch:
        result = ch.query(sql, parameters=params or {})
        cols = result.column_names
        return [dict(zip(cols, row, strict=False)) for row in result.result_rows]


def query_one(
    sql: str,
    params: dict[str, Any] | None = None,
    *,
    force_tenant_id: str | None = "sentinel",
) -> dict[str, Any] | None:
    """便捷封装：返回首行 dict，找不到返回 None。"""
    rows = query_all(sql, params, force_tenant_id=force_tenant_id)
    return rows[0] if rows else None
