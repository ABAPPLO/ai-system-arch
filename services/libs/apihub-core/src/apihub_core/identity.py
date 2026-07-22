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
from apihub_core.l1 import TTLCache

_identity_l1: TTLCache | None = None
_secret_l1: TTLCache | None = None


def configure_l1(*, identity: TTLCache | None = None, secret: TTLCache | None = None) -> None:
    """opt-in L1（dispatcher 进程注入）。None = 关。默认全 None（不改变既有行为）。"""
    global _identity_l1, _secret_l1
    _identity_l1 = identity
    _secret_l1 = secret


def identity_cache_key(api_key: str) -> str:
    """ak:{sha256(api_key)}。"""
    return "ak:" + hashlib.sha256(api_key.encode("utf-8")).hexdigest()


async def _parse_identity(api_key: str, raw: object) -> dict[str, Any] | None:
    """Redis 原始值 → identity dict（损坏/非 dict → 清缓存返 None）。"""
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        await redis.raw_client().delete(identity_cache_key(api_key))
        return None
    if not isinstance(data, dict):
        # 非字典（如 "42"、[1,2]）——视同损坏，清除并 miss，避免调用方 .get 抛 AttributeError/500
        await redis.raw_client().delete(identity_cache_key(api_key))
        return None
    return data


async def read_identity(api_key: str) -> dict[str, Any] | None:
    """读身份缓存。dict（含可能 {"invalid": True}）或 None（miss/损坏）。"""
    if _identity_l1 is not None:
        cached = _identity_l1.get(api_key)
        if isinstance(cached, dict):
            return cached
    raw = await redis.raw_client().get(identity_cache_key(api_key))
    parsed = await _parse_identity(api_key, raw)
    if _identity_l1 is not None and isinstance(parsed, dict):
        _identity_l1.set(api_key, parsed)
    return parsed


async def write_identity(api_key: str, data: dict[str, Any], ttl: int) -> None:
    await redis.raw_client().setex(identity_cache_key(api_key), ttl, json.dumps(data))


async def delete_identity(api_key: str) -> None:
    if _identity_l1 is not None:
        _identity_l1.invalidate(api_key)
    await redis.raw_client().delete(identity_cache_key(api_key))


def hmac_secret_cache_key(api_key: str) -> str:
    """hmac_secret:{sha256(api_key)} —— 与 identity 分键，便于 rotate 只清 secret。"""
    return "hmac_secret:" + hashlib.sha256(api_key.encode("utf-8")).hexdigest()


async def write_hmac_secret(api_key: str, secret_encrypted: str, ttl: int) -> None:
    """写加密 secret blob（不存明文）。caller = auth create/rotate。"""
    await redis.raw_client().setex(hmac_secret_cache_key(api_key), ttl, secret_encrypted)


async def read_hmac_secret(api_key: str) -> str | None:
    """读加密 secret blob（miss/损坏返 None）。caller = dispatcher，须自行 decrypt。"""
    if _secret_l1 is not None:
        cached = _secret_l1.get(api_key)
        if isinstance(cached, str):
            return cached
    raw = await redis.raw_client().get(hmac_secret_cache_key(api_key))
    val = raw if isinstance(raw, str) else None
    if _secret_l1 is not None and val is not None:
        _secret_l1.set(api_key, val)
    return val


async def delete_hmac_secret(api_key: str) -> None:
    if _secret_l1 is not None:
        _secret_l1.invalidate(api_key)
    await redis.raw_client().delete(hmac_secret_cache_key(api_key))


async def read_identity_and_hmac_secret(
    api_key: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """L1 优先；任一 miss → 批 pipeline Redis（1 RTT）。投机取 secret（unenrolled → None）。"""
    ident: dict[str, Any] | None = None
    sec: str | None = None
    if _identity_l1 is not None:
        hit = _identity_l1.get(api_key)
        if isinstance(hit, dict):
            ident = hit
    if _secret_l1 is not None:
        hit = _secret_l1.get(api_key)
        if isinstance(hit, str):
            sec = hit
    need: list[tuple[str, str]] = []
    if ident is None:
        need.append(("ident", identity_cache_key(api_key)))
    if sec is None:
        need.append(("secret", hmac_secret_cache_key(api_key)))
    if not need:
        return ident, sec
    pipe = redis.raw_client().pipeline()
    for _, k in need:
        pipe.get(k)
    results = await pipe.execute()
    raw_map = {need[i][0]: results[i] for i in range(len(need))}
    if "ident" in raw_map:
        ident = await _parse_identity(api_key, raw_map["ident"])
        if _identity_l1 is not None and isinstance(ident, dict):
            _identity_l1.set(api_key, ident)
    if "secret" in raw_map:
        v = raw_map["secret"]
        sec = v if isinstance(v, str) else None
        if _secret_l1 is not None and sec is not None:
            _secret_l1.set(api_key, sec)
    return ident, sec
