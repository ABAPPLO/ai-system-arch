"""路由解析 —— 把 incoming 请求映射到 ApiVersionSnapshot。

唯一入口：X-API-Version-Id header（APISIX 在 auth 阶段已注入）。
路径反查（resolve_by_path）已移除：dispatcher 退纯转发，所有路由由 APISIX 下发。

resolve 阶段使用 meta_db_session（绕 RLS）跨租户可见所有 published API/api_version
元数据 —— 因为路由解析是平台网关职责，external-public caller 也要能 resolve 到
tenant_a 的 public API。授权（public/tenant/private 三级）由应用层
dispatcher.visibility.check_visibility 在转发前做。
"""

import dataclasses
import json

from apihub_core import db, redis
from apihub_core.errors import ApiError, ErrorCode

from dispatcher.models import ApiVersionSnapshot


async def resolve_by_header(version_id: str) -> ApiVersionSnapshot:
    """APISIX 注入的 X-API-Version-Id → 按 ID 查 + Redis 缓存。

    生命周期：published/deprecated 可路由；retired → 410 Gone；其余 → 404。
    """
    cache_key = f"snapshot:{version_id}"
    cached = await redis.t_get(cache_key)
    if cached:
        # 防缓存陈旧：cached snapshot 无 status，retire 后最多 5 分钟仍命中。
        # 命中时用一次 PK 状态查询兜底：retired→410，并清 stale 缓存。
        async with db.meta_db_session() as conn:
            status = await conn.fetchval(
                "SELECT status FROM api_version WHERE id = $1", version_id
            )
        if status == "retired":
            await redis.t_delete(cache_key)
            raise ApiError(ErrorCode.API_RETIRED, f"version {version_id} retired")
        if status not in ("published", "deprecated"):
            await redis.t_delete(cache_key)
            raise ApiError(
                ErrorCode.API_NOT_PUBLISHED, f"version {version_id} not published"
            )
        return _from_json(json.loads(cached))

    async with db.meta_db_session() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, api_id, tenant_id, version, backend_type, backend_url,
                   method, path, masking, rate_limit, retry_policy, cache_policy,
                   ai_model, ai_streaming, ai_params, sla_p99_ms, sla_availability
            FROM api_version
            WHERE id = $1 AND status IN ('published', 'deprecated')
            """,
            version_id,
        )
        if not row:
            status = await conn.fetchval(
                "SELECT status FROM api_version WHERE id = $1", version_id
            )
            if status == "retired":
                raise ApiError(ErrorCode.API_RETIRED, f"version {version_id} retired")
            raise ApiError(ErrorCode.API_NOT_PUBLISHED, f"version {version_id} not published")

    _, visibility = await _get_api_meta(row["api_id"])
    snapshot = _from_row(row, visibility=visibility)
    await redis.t_set(cache_key, json.dumps(dataclasses.asdict(snapshot)), ex=300)
    return snapshot


async def _get_api_meta(api_id: str) -> tuple[str | None, str]:
    """从 api 表取 (base_path, visibility)。

    路由解析需要跨租户可见性（public API 也位于任意租户下），故走 meta_db_session。
    行不存在时返回 (None, 'private')：base_path=None 让调用方 skip 该候选，
    visibility 取最严格的 'private' 做防御性默认。
    """
    from apihub_core import db as _db

    async with _db.meta_db_session() as conn:
        row = await conn.fetchrow(
            "SELECT base_path, visibility FROM api WHERE id = $1", api_id
        )
    if not row:
        return None, "private"
    return row["base_path"], row["visibility"]


def _extract_path_params(pattern: str, actual: str) -> dict[str, str]:
    params: dict[str, str] = {}
    for p, a in zip(pattern.strip("/").split("/"), actual.strip("/").split("/"), strict=False):
        if p.startswith("{") and p.endswith("}"):
            params[p[1:-1]] = a
    return params


def _from_row(row, visibility: str = "private") -> ApiVersionSnapshot:
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
        visibility=visibility,
    )


def _from_json(data: dict) -> ApiVersionSnapshot:
    return ApiVersionSnapshot(**data)
