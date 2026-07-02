"""Redis 缓存读写 —— 用 raw_client（跨租户，因为 key 本身就是身份证明）。

缓存策略：
  - 正缓存（合法 key）: 5 分钟
  - 负缓存（非法 key）: 1 分钟（防爆破）
  - 吊销时主动 DEL

key 格式：ak:{sha256_of_plaintext}
value 格式：JSON
"""

import json
from typing import Any

from apihub_core import redis

from auth.apikey import (
    NEGATIVE_CACHE_TTL,
    POSITIVE_CACHE_TTL,
    cache_key,
)


async def cache_positive(api_key_plaintext: str, data: dict[str, Any]) -> None:
    """缓存合法 key 的解析结果。"""
    key = cache_key(api_key_plaintext)
    await redis.raw_client().setex(key, POSITIVE_CACHE_TTL, json.dumps(data))


async def cache_negative(api_key_plaintext: str) -> None:
    """缓存非法 key 的负结果（避免反复打 DB）。"""
    key = cache_key(api_key_plaintext)
    await redis.raw_client().setex(key, NEGATIVE_CACHE_TTL, json.dumps({"invalid": True}))


async def get_cached(api_key_plaintext: str) -> dict[str, Any] | None:
    """读缓存。返回 dict 或 None。

    返回的 dict 可能包含 {"invalid": True}（负缓存），调用方应区分。
    """
    key = cache_key(api_key_plaintext)
    raw = await redis.raw_client().get(key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # 缓存损坏，删掉
        await redis.raw_client().delete(key)
        return None


async def invalidate(api_key_plaintext_or_hash: str) -> None:
    """吊销时主动清缓存。"""
    key = cache_key(api_key_plaintext_or_hash)
    await redis.raw_client().delete(key)


async def warmup(api_key_plaintext: str, data: dict[str, Any]) -> None:
    """预加载缓存（启动时或首次访问时）。"""
    await cache_positive(api_key_plaintext, data)
