"""PostgreSQL 连接 + RLS 会话上下文。

核心思想：每次请求把 tenant_id 设到 DB session（SET LOCAL），结合 RLS 策略，
即使业务代码忘了加 WHERE tenant_id=? 也不会泄漏。

详见 docs/04-data-model.md §5 RLS 策略。
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import asyncpg

from apihub_core.config import Settings
from apihub_core.logging import get_logger
from apihub_core.tenant import get_tenant_context

log = get_logger(__name__)

_pool: asyncpg.Pool | None = None

_AUDIT_TABLE = "audit_log"
_audit_reason_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "audit_reason", default=None
)

# 统一 JSON encoder：调用方直传 dict，codec 负责序列化并兜底非 JSON 原生类型
# （datetime / UUID / set 等 → str）。`default=str` 保留旧 `json.dumps(x, default=str)`
# helper 的兜底语义，对已可序列化数据零行为变化。
_JSON_ENCODER = functools.partial(json.dumps, default=str)


def set_audit_reason(reason: str | None) -> contextvars.Token[str | None]:
    """在当前协程内设默认审计 reason（HTTP 中间件用，免去逐调用传参）。"""
    return _audit_reason_var.set(reason)


def reset_audit_reason(token: contextvars.Token[str | None]) -> None:
    _audit_reason_var.reset(token)


async def _init_jsonb_codec(conn: asyncpg.Connection) -> None:
    """让 jsonb 列直接返回 dict（decoder=json.loads），encoder 用 json.dumps+default=str。

    每个新连接都会跑一次（create_pool 的 init 回调）。

    调用方应直传 dict 让 codec 序列化；切勿调用方先 `json.dumps(...)` 再传 str 给
    `$N::jsonb` —— 生产 pool 经本函数注册的 codec 会再 encode 一次该字符串，
    产出 JSON 字符串字面量 → PG 存 jsonb 类型为 `string`（非 `object`）→
    `detail->>'...'` / `metadata->'quota'->>'day_limit'` 等返回 NULL。
    单元测试若 create_pool 未带 init=_init_jsonb_codec，asyncpg 走 text fallback
    会掩盖此 bug（仅生产暴露）。参考：workflow_svc/repository.py:35-37 直传 dict。
    """
    await conn.set_type_codec(
        "jsonb",
        encoder=_JSON_ENCODER,
        decoder=json.loads,
        schema="pg_catalog",
    )
    await conn.set_type_codec(
        "json",
        encoder=_JSON_ENCODER,
        decoder=json.loads,
        schema="pg_catalog",
    )


async def init_pool(settings: Settings) -> None:
    """进程启动时调一次。

    启动建连带退避重试：kind 等 CNI/DNS 抢跑环境下，新 pod 首次解析 PG host 可能
    EAI_AGAIN（socket.gaierror，OSError 子类）或连接被拒——退避重试而非首枪崩溃
    （否则 startupProbe 期内起不来 → CrashLoopBackOff）。窗口 = retries × backoff
    （默认 10 × 1.5s ≈ 15s，落在各服务 startupProbe 的 120s 内）。
    """
    global _pool
    # asyncpg 接受 'disable'/'prefer'/'require'/'verify-ca'/'verify-full'/False/True
    # 这里直接透传 settings.pg_ssl 字符串；False 表示完全关闭。
    ssl_value: str | bool = (
        False if settings.pg_ssl.lower() in ("false", "off", "no") else settings.pg_ssl
    )

    last_exc: Exception | None = None
    for attempt in range(1, settings.startup_connect_retries + 1):
        pool: asyncpg.Pool | None = None
        try:
            pool = await asyncpg.create_pool(
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
            # 预热：asyncpg create_pool 惰性建连，连接到首次 acquire 才真正建立
            # （host 解析也在此刻）。并发持有 min_size 条再释放，强制启动期建好，
            # 让首个请求即走热连接。
            held = [await pool.acquire() for _ in range(settings.pg_pool_min)]
            for conn in held:
                await pool.release(conn)
            _pool = pool
            return
        except (OSError, asyncpg.PostgresError) as e:
            # OSError 覆盖 socket.gaierror(EAI_AGAIN)/ConnectionRefusedError 等
            last_exc = e
            if pool is not None:
                with suppress(Exception):
                    await pool.close()
            log.warning(
                "db_init_retry",
                attempt=attempt,
                of=settings.startup_connect_retries,
                error=f"{type(e).__name__}: {e}",
            )
            if attempt >= settings.startup_connect_retries:
                break
            await asyncio.sleep(settings.startup_connect_backoff)
    assert last_exc is not None
    raise last_exc


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


async def _write_admin_audit(reason: str) -> None:
    """用独立 raw 连接写一条审计（避免走 admin_db_session 递归）。best-effort。

    admin/repository.record() 本身走 admin_db_session 写 audit_log；若本函数也走
    admin_db_session 会无限递归。故单独 acquire 连接、单独事务、失败只 log。
    """
    import structlog

    log = structlog.get_logger("apihub_core.db")
    if _pool is None:
        return
    ctx = get_tenant_context()
    tenant_id = ctx.tenant_id if ctx else ""
    try:
        async with _pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT set_config('app.is_platform_admin', $1, true)", "true"
            )
            # 传 dict 让 asyncpg 的 jsonb codec（init_pool 注册的 encoder=_JSON_ENCODER，
            # 即 json.dumps + default=str）序列化；若在此处预先 json.dumps 得到 str，
            # codec 会再次 JSON-encode 该字符串，写入 jsonb 类型变为 JSON 字符串值
            # （kind=string）而非对象 → detail->>'reason' 返回 NULL。
            # 单元测试无 codec 时走 fallback text 编码看不出此 bug，仅 e2e 暴露。
            await conn.execute(
                f"""
                INSERT INTO {_AUDIT_TABLE}
                    (tenant_id, actor_type, action, resource_type, detail)
                VALUES ($1, 'system', 'admin_db_session', 'platform', $2::jsonb)
                """,
                tenant_id,
                {"reason": reason},
            )
    except Exception as e:  # best-effort：审计失败不能影响业务
        log.warning("admin_audit_write_failed", error=str(e), reason=reason)


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
async def admin_db_session(
    *, audit_reason: str | None = None
) -> AsyncIterator[asyncpg.Connection]:
    """超管 DB 会话 —— 绕过 RLS，可见所有租户数据。

    使用场景（仅限平台运维 + 几个特殊服务）：
      - auth 服务跨租户查 api_key（APIKey → tenant_id/app_id）
      - 平台运维跨租户排查
      - 审计聚合查询

    ⚠️ 业务代码禁用。审计是 **opt-in**：传 `audit_reason`（或经 `set_audit_reason`
    设了 contextvar）才写一条 audit_log（action='admin_db_session'）。审计用独立
    raw 连接写入、best-effort，不影响本会话事务，也不会递归（区别于
    admin/repository.record() 显式写审计的路径）。
    """
    if _pool is None:
        raise RuntimeError("DB pool not initialized. Call init_pool first.")

    reason = audit_reason or _audit_reason_var.get()
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
    if reason:
        await _write_admin_audit(reason)


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
