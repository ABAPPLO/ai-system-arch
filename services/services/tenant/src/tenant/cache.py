"""租户元数据缓存 —— auth / quota / dispatcher 都会读。

key 格式：t:{tenant_id}:meta
TTL：30min（docs/11-multi-tenant.md §8.3）
失效：状态变更（suspend/resume/close）+ 配额变更（PUT /quota）

设计要点：
  - 缓存层不解析字段，只存 dict（PG row 的 JSON）
  - 调用方拿到 None 时去查 PG，然后 warmup
  - "suspended" 状态也缓存，让上游服务快速拒绝请求
"""

import json
from typing import Any

from apihub_core import redis

CACHE_TTL_SECONDS = 30 * 60


def _key(tenant_id: str) -> str:
    return f"t:{tenant_id}:meta"


async def get(tenant_id: str) -> dict[str, Any] | None:
    """读缓存。返回 dict 或 None。"""
    raw = await redis.raw_client().get(_key(tenant_id))
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        await redis.raw_client().delete(_key(tenant_id))
        return None


async def set(tenant_id: str, data: dict[str, Any]) -> None:
    """warmup / 刷新。"""
    await redis.raw_client().setex(
        _key(tenant_id), CACHE_TTL_SECONDS, json.dumps(data, default=str)
    )


async def invalidate(tenant_id: str) -> None:
    """状态/配额变更时主动清缓存。"""
    await redis.raw_client().delete(_key(tenant_id))


async def invalidate_many(tenant_ids: list[str]) -> None:
    """批量失效（如关闭父租户时清子）。"""
    if not tenant_ids:
        return
    keys = [_key(tid) for tid in tenant_ids]
    await redis.raw_client().delete(*keys)
