"""PostgreSQL 连接 + RLS 会话上下文。

核心思想：每次请求把 tenant_id 设到 DB session（SET LOCAL），结合 RLS 策略，
即使业务代码忘了加 WHERE tenant_id=? 也不会泄漏。

详见 docs/04-data-model.md §5 RLS 策略。
"""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg

from apihub_core.config import Settings
from apihub_core.tenant import get_tenant_context

_pool: asyncpg.Pool | None = None


async def _init_jsonb_codec(conn: asyncpg.Connection) -> None:
    """让 jsonb 列直接返回 dict，避免每个 repository 都要 json.loads。

    asyncpg 默认把 jsonb 当 text 返回；这里注册 codec 让它走 json.loads。
    每个新连接都会跑一次（create_pool 的 init 回调）。
    """
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )
    await conn.set_type_codec(
        "json",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )


async def init_pool(settings: Settings) -> None:
    """进程启动时调一次。"""
    global _pool
    # asyncpg 接受 'disable'/'prefer'/'require'/'verify-ca'/'verify-full'/False/True
    # 这里直接透传 settings.pg_ssl 字符串；False 表示完全关闭。
    ssl_value: str | bool = (
        False if settings.pg_ssl.lower() in ("false", "off", "no") else settings.pg_ssl
    )

    _pool = await asyncpg.create_pool(
        host=settings.pg_host,
        port=settings.pg_port,
        database=settings.pg_database,
        user=settings.pg_user,
        password=settings.pg_password,
        min_size=settings.pg_pool_min,
        max_size=settings.pg_pool_max,
        ssl=ssl_value,
        statement_cache_size=100,
        init=_init_jsonb_codec,
    )

    # 预热：asyncpg create_pool 惰性建连，连接到首次 acquire 才真正建立。
    # 进程刚起（如 ArgoCD resync 重启后）首个 auth verify 若是 cache-miss，
    # 会付冷建连费 —— kind/host-compose 下实测 3-15s，直接撞调用方 httpx timeout
    # → 503 "Auth service unreachable"。并发持有 min_size 条再释放，强制启动期建好，
    # 让首个请求即走热连接（配合各服务 startupProbe 的 120s 窗口吸收建连耗时）。
    held = []
    for _ in range(settings.pg_pool_min):
        held.append(await _pool.acquire())
    for conn in held:
        await _pool.release(conn)


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
                # 注入租户上下文给 RLS 用（参数化，防 SQL 注入 —— R0a §2.5）
                await conn.execute(
                    "SELECT set_config('app.tenant_id', $1, true)", ctx.tenant_id
                )
                await conn.execute(
                    "SELECT set_config('app.is_platform_admin', $1, true)",
                    "true" if ctx.is_platform_admin else "false",
                )
                yield conn
                await tr.commit()
            except Exception:
                await tr.rollback()
                raise
        else:
            yield conn


@asynccontextmanager
async def admin_db_session() -> AsyncIterator[asyncpg.Connection]:
    """超管 DB 会话 —— 绕过 RLS，可见所有租户数据。

    使用场景（仅限平台运维 + 几个特殊服务）：
      - auth 服务跨租户查 api_key（APIKey → tenant_id/app_id）
      - 平台运维跨租户排查
      - 审计聚合查询

    ⚠️ 业务代码禁用，每次调用都会写 audit_events（外部可观测）。
    """
    if _pool is None:
        raise RuntimeError("DB pool not initialized. Call init_pool first.")

    async with _pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            await conn.execute(
                "SELECT set_config('app.is_platform_admin', $1, true)", "true"
            )
            yield conn
            await tr.commit()
        except Exception:
            await tr.rollback()
            raise


@asynccontextmanager
async def meta_db_session() -> AsyncIterator[asyncpg.Connection]:
    """平台元数据查询会话 —— 绕过 RLS，可见所有租户元数据。

    仅供平台网关职责（如 dispatcher 路由解析）跨租户查 published API/api_version
    元数据，授权由应用层（dispatcher visibility 检查）做。不写审计（区别于
    admin_db_session 的人工运维/审计场景）。业务代码禁用。

    与 admin_db_session 的区别：admin_db_session 面向人工运维 + 审计聚合，
    每次调用语义上对应一次可追溯的操作；meta_db_session 面向无租户偏好的平台
    网关读路径（路由解析），是纯元数据查询，不构成需要审计的业务行为。
    """
    if _pool is None:
        raise RuntimeError("DB pool not initialized. Call init_pool first.")
    async with _pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            await conn.execute(
                "SELECT set_config('app.is_platform_admin', $1, true)", "true"
            )
            yield conn
            await tr.commit()
        except Exception:
            await tr.rollback()
            raise
