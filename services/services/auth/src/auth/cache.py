"""Redis 缓存读写 —— 委托 apihub_core.identity（单一真相源）。

缓存策略：
  - 正缓存（合法 key）: 5 分钟
  - 负缓存（非法 key）: 1 分钟（防爆破）
  - 吊销时主动 DEL

key/value 契约见 apihub_core.identity。
"""

from typing import Any

from apihub_core import identity

from auth.apikey import (
    NEGATIVE_CACHE_TTL,
    POSITIVE_CACHE_TTL,
    cache_key,
)


async def cache_positive(api_key_plaintext: str, data: dict[str, Any]) -> None:
    await identity.write_identity(api_key_plaintext, data, ttl=POSITIVE_CACHE_TTL)


async def cache_negative(api_key_plaintext: str) -> None:
    await identity.write_identity(api_key_plaintext, {"invalid": True}, ttl=NEGATIVE_CACHE_TTL)


async def get_cached(api_key_plaintext: str) -> dict[str, Any] | None:
    return await identity.read_identity(api_key_plaintext)


async def invalidate(api_key_plaintext_or_hash: str) -> None:
    """吊销时主动清缓存。入参可为明文或 hash（cache_key 两者兼容）。"""
    # revoke_key 传 key_hash（非明文），identity.delete_identity 用明文算 key —— 故此处
    # 仍走 auth.apikey.cache_key（兼容明文/-hash），直接操作 raw_client，保持原行为。
    from apihub_core import redis

    await redis.raw_client().delete(cache_key(api_key_plaintext_or_hash))


async def warmup(api_key_plaintext: str, data: dict[str, Any]) -> None:
    """预加载缓存（启动时或首次访问时）。"""
    await cache_positive(api_key_plaintext, data)
