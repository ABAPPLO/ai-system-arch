"""Redis 身份缓存契约 —— auth 写、dispatcher 读（信任路径），单一真相源。

key: ak:{sha256(api_key_plaintext)}   （与 auth.apikey.cache_key 一致）
value: JSON dict = VerifyResponse 字段（is_active/tenant_id/tenant_type/app_id/
       is_platform_admin/scopes/expires_at），或 {"invalid": True}（负缓存）。
用 redis.raw_client()（无租户前缀，因 key 本身即身份证明）。
"""

import hashlib
import json
from typing import Any

from apihub_core import redis


def identity_cache_key(api_key: str) -> str:
    """ak:{sha256(api_key)}。"""
    return "ak:" + hashlib.sha256(api_key.encode("utf-8")).hexdigest()


async def read_identity(api_key: str) -> dict[str, Any] | None:
    """读身份缓存。dict（含可能 {"invalid": True}）或 None（miss/损坏）。"""
    raw = await redis.raw_client().get(identity_cache_key(api_key))
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        await redis.raw_client().delete(identity_cache_key(api_key))
        return None


async def write_identity(api_key: str, data: dict[str, Any], ttl: int) -> None:
    await redis.raw_client().setex(identity_cache_key(api_key), ttl, json.dumps(data))


async def delete_identity(api_key: str) -> None:
    await redis.raw_client().delete(identity_cache_key(api_key))
