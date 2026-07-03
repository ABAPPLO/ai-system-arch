"""Redis 客户端 — 租户前缀封装。

key 规范：`t:{tenant_id}:{namespace}:{key}`
详见 docs/04-data-model.md §7 Redis 键空间。
"""


import redis.asyncio as redis

from apihub_core.config import Settings
from apihub_core.tenant import get_tenant_context

_client: redis.Redis | None = None


async def init_redis(settings: Settings) -> None:
    global _client
    _client = redis.Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        password=settings.redis_password,
        ssl=settings.redis_ssl,
        decode_responses=True,
        max_connections=200,
    )


async def close_redis() -> None:
    global _client
    if _client:
        await _client.aclose()
        _client = None


def _prefix(key: str) -> str:
    ctx = get_tenant_context()
    if ctx:
        return f"{ctx.key_prefix}{key}"
    return key


async def t_get(key: str) -> str | None:
    if _client is None:
        raise RuntimeError("Redis not initialized")
    return await _client.get(_prefix(key))


async def t_set(key: str, value: str, ex: int | None = None) -> None:
    if _client is None:
        raise RuntimeError("Redis not initialized")
    await _client.set(_prefix(key), value, ex=ex)


async def t_incr(key: str, amount: int = 1) -> int:
    if _client is None:
        raise RuntimeError("Redis not initialized")
    return await _client.incrby(_prefix(key), amount)


async def t_expire(key: str, seconds: int) -> None:
    if _client is None:
        raise RuntimeError("Redis not initialized")
    await _client.expire(_prefix(key), seconds)


async def t_delete(key: str) -> None:
    if _client is None:
        raise RuntimeError("Redis not initialized")
    await _client.delete(_prefix(key))


def raw_client() -> redis.Redis:
    """跨租户操作时（仅平台运维）使用。"""
    if _client is None:
        raise RuntimeError("Redis not initialized")
    return _client
