"""路由解析 —— 把 incoming 请求映射到 ApiVersionSnapshot。

优先级：
1. X-API-Version-Id header（APISIX 在 auth 阶段已注入，最快）
2. Path + Method 匹配（开发直连 dispatcher 时回退）
"""

import json

from apihub_core import db, redis
from apihub_core.errors import ApiError, ErrorCode
from apihub_core.tenant import require_tenant

from dispatcher.models import ApiVersionSnapshot


async def resolve_by_header(version_id: str) -> ApiVersionSnapshot:
    """优先路径：APISIX 注入了 X-API-Version-Id，直接按 ID 查 + Redis 缓存。"""
    cache_key = f"snapshot:{version_id}"
    cached = await redis.t_get(cache_key)
    if cached:
        return _from_json(json.loads(cached))

    async with db.db_session() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, api_id, tenant_id, version, backend_type, backend_url,
                   method, path, masking, rate_limit, retry_policy, cache_policy,
                   ai_model, ai_streaming, ai_params, sla_p99_ms, sla_availability
            FROM api_version WHERE id = $1 AND status = 'published'
            """,
            version_id,
        )

    if not row:
        raise ApiError(ErrorCode.API_NOT_PUBLISHED, f"version {version_id} not published")

    snapshot = _from_row(row)
    await redis.t_set(cache_key, json.dumps(snapshot.__dict__), ex=300)
    return snapshot


async def resolve_by_path(method: str, full_path: str) -> ApiVersionSnapshot:
    """回退路径：无 header 时按 path 反查。性能差，仅用于 dev / 直连。"""
    require_tenant()

    async with db.db_session() as conn:
        rows = await conn.fetch(
            """
            SELECT id, api_id, tenant_id, version, backend_type, backend_url,
                   method, path, masking, rate_limit, retry_policy, cache_policy,
                   ai_model, ai_streaming, ai_params, sla_p99_ms, sla_availability
            FROM api_version
            WHERE status = 'published' AND method = $1
            """,
            method.upper(),
        )

    for row in rows:
        api_base = await _get_base_path(conn_pool=None, api_id=row["api_id"])
        if not api_base:
            continue
        full_pattern = f"{api_base}{row['path']}"
        if _match_path(full_pattern, full_path):
            return _from_row(row)

    raise ApiError(ErrorCode.API_NOT_FOUND, f"no API matches {method} {full_path}")


async def _get_base_path(conn_pool, api_id: str) -> str | None:
    """从 api 表取 base_path（dev 回退路径才用，prod 走 header）。"""
    from apihub_core import db as _db

    async with _db.db_session() as conn:
        row = await conn.fetchrow("SELECT base_path FROM api WHERE id = $1", api_id)
    return row["base_path"] if row else None


def _match_path(pattern: str, actual: str) -> bool:
    """简单 path 匹配：{var} 通配一段。

    /v1/users/{user_id}  匹配  /v1/users/u_001
    /v1/users/{user_id}  不匹配 /v1/users/u_001/orders
    """
    pp = pattern.strip("/").split("/")
    aa = actual.strip("/").split("/")
    if len(pp) != len(aa):
        return False
    for p, a in zip(pp, aa, strict=True):
        if p.startswith("{") and p.endswith("}"):
            continue
        if p != a:
            return False
    return True


def _extract_path_params(pattern: str, actual: str) -> dict[str, str]:
    params: dict[str, str] = {}
    for p, a in zip(pattern.strip("/").split("/"), actual.strip("/").split("/"), strict=False):
        if p.startswith("{") and p.endswith("}"):
            params[p[1:-1]] = a
    return params


def _from_row(row) -> ApiVersionSnapshot:
    return ApiVersionSnapshot(
        id=row["id"],
        api_id=row["api_id"],
        tenant_id=row["tenant_id"],
        version=row["version"],
        backend_type=row["backend_type"],
        backend_url=row["backend_url"],
        method=row["method"],
        path=row["path"],
        masking=row["masking"],
        rate_limit=row["rate_limit"],
        retry_policy=row["retry_policy"],
        cache_policy=row["cache_policy"],
        ai_model=row["ai_model"],
        ai_streaming=row["ai_streaming"],
        ai_params=row["ai_params"],
        sla_p99_ms=row["sla_p99_ms"],
        sla_availability=row["sla_availability"],
    )


def _from_json(data: dict) -> ApiVersionSnapshot:
    return ApiVersionSnapshot(**data)
