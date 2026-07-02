"""PostgreSQL 连接 + RLS 会话上下文。

核心思想：每次请求把 tenant_id 设到 DB session（SET LOCAL），结合 RLS 策略，
即使业务代码忘了加 WHERE tenant_id=? 也不会泄漏。

详见 docs/04-data-model.md §5 RLS 策略。
"""

from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

import asyncpg

from apihub_core.config import Settings
from apihub_core.tenant import get_tenant_context


_pool: Optional[asyncpg.Pool] = None


async def init_pool(settings: Settings) -> None:
    """进程启动时调一次。"""
    global _pool
    _pool = await asyncpg.create_pool(
        host=settings.pg_host,
        port=settings.pg_port,
        database=settings.pg_database,
        user=settings.pg_user,
        password=settings.pg_password,
        min_size=settings.pg_pool_min,
        max_size=settings.pg_pool_max,
        ssl="require",
        statement_cache_size=100,
    )


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def db_session() -> AsyncIterator[asyncpg.Connection]:
    """租户感知的 DB 会话。

    用法：
        async with db_session() as conn:
            rows = await conn.fetch("SELECT * FROM api WHERE id = $1", api_id)

    框架会自动：
      1. 取当前协程的 TenantContext
      2. 在事务里 SET LOCAL app.tenant_id = ?
      3. RLS 策略据此过滤
    """
    if _pool is None:
        raise RuntimeError("DB pool not initialized. Call init_pool first.")

    async with _pool.acquire() as conn:
        ctx = get_tenant_context()
        if ctx:
            tr = conn.transaction()
            await tr.start()
            try:
                # 注入租户上下文给 RLS 用
                await conn.execute(f"SET LOCAL app.tenant_id = '{ctx.tenant_id}'")
                await conn.execute(
                    f"SET LOCAL app.is_platform_admin = '{ctx.is_platform_admin}'"
                )
                yield conn
                await tr.commit()
            except Exception:
                await tr.rollback()
                raise
        else:
            yield conn
