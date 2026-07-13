"""portal app/key 自助 —— 直写 app/api_key 表（RLS 按 caller tenant 隔离）。

不复用 auth /v1/apps 端点：那个走 X-API-Key middleware，而 Portal 是 JWT 人认证。
"""
# ruff: noqa: S608

import secrets
from typing import Any

import httpx
from apihub_core import db
from apihub_core.config import get_settings
from apihub_core.errors import ApiError, ErrorCode

from portal.models import (
    PortalApiDetail,
    PortalApiItem,
    PortalApiListResponse,
    PortalVersionItem,
    TryRequest,
    TryResponse,
)


async def create_app_for_user(*, tenant_id: str, name: str, app_type: str) -> dict:
    app_id = f"app_{secrets.token_hex(8)}"
    async with db.db_session() as conn:
        await conn.execute(
            """
            INSERT INTO app (id, tenant_id, name, type, status)
            VALUES ($1, $2, $3, $4, 'active')
            """,
            app_id,
            tenant_id,
            name,
            app_type,
        )
    return {
        "id": app_id,
        "name": name,
        "tenant_id": tenant_id,
        "type": app_type,
        "status": "active",
    }


async def list_apps_for_user(*, tenant_id: str) -> list[dict]:
    async with db.db_session() as conn:
        rows = await conn.fetch(
            "SELECT id, name, tenant_id, type, status FROM app "
            "WHERE tenant_id=$1 ORDER BY created_at DESC",
            tenant_id,
        )
    return [dict(r) for r in rows]


async def create_api_key_for_app(
    *, tenant_id: str, app_id: str, name: str
) -> dict:
    from auth.apikey import generate_api_key  # 复用 auth 的 key 生成纯函数

    plaintext, key_hash, display_prefix = generate_api_key()
    key_id = f"key_{secrets.token_hex(8)}"
    async with db.db_session() as conn:
        # app 归属校验：db_session 已 SET LOCAL app.tenant_id=caller，RLS 自动过滤
        # 跨租户 app，查不到即非本租户（或不存在）→ 404，避免悬空 api_key 行。
        belongs = await conn.fetchval("SELECT 1 FROM app WHERE id = $1", app_id)
        if not belongs:
            raise ApiError(
                ErrorCode.NOT_FOUND,
                "app not found in your tenant",
                http_status=404,
            )
        await conn.execute(
            """
            INSERT INTO api_key (id, tenant_id, app_id, key_prefix, key_hash, name, status)
            VALUES ($1, $2, $3, $4, $5, $6, 'active')
            """,
            key_id,
            tenant_id,
            app_id,
            display_prefix,
            key_hash,
            name,
        )
    return {
        "id": key_id,
        "app_id": app_id,
        "name": name,
        "key_prefix": display_prefix,
        "api_key": plaintext,
    }


async def list_portal_apis(
    search: str = "",
    category: str = "",
    tag: str = "",
    limit: int = 50,
    offset: int = 0,
) -> PortalApiListResponse:
    """API 目录列表 + 搜索/过滤/分页。

    通过 db_session (RLS) 自动按 caller 租户过滤可见 API。
    """
    search_clause = ""
    params: list[Any] = []
    idx = 1

    if search:
        search_clause = f" AND (a.name ILIKE ${idx} OR a.description ILIKE ${idx})"
        params.append(f"%{search}%")
        idx += 1
    if category:
        search_clause += f" AND a.category = ${idx}"
        params.append(category)
        idx += 1
    if tag:
        search_clause += f" AND ${idx} = ANY(a.tags)"
        params.append(tag)
        idx += 1

    async with db.db_session() as conn:
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM api a WHERE a.status = 'published'" + search_clause,  # noqa: S608 — search_clause is built from fixed strings with $N parameterized placeholders only, no user data
            *params,
        )

        _order = f" ORDER BY a.updated_at DESC LIMIT ${idx} OFFSET ${idx + 1}"
        list_sql = (
            "SELECT a.id, a.name, a.description, a.category, a.tags,"
            "       a.base_path, a.visibility, v.backend_type, v.version, a.updated_at"
            " FROM api a"
            " LEFT JOIN LATERAL ("
            "     SELECT version, backend_type FROM api_version"
            "     WHERE api_id = a.id AND status = 'published'"
            "     ORDER BY created_at DESC LIMIT 1"
            " ) v ON true"
            " WHERE a.status = 'published'" + search_clause + _order
        )
        params.append(limit)
        params.append(offset)
        rows = await conn.fetch(list_sql, *params)

    items: list[PortalApiItem] = []
    all_categories: set[str] = set()
    all_tags: set[str] = set()
    for r in rows:
        raw_tags = r.get("tags") or []
        tags_list: list[str] = [str(t) for t in raw_tags] if isinstance(raw_tags, (list, tuple)) else []
        items.append(PortalApiItem(
            api_id=str(r["id"]),
            name=r["name"],
            description=r["description"],
            category=r["category"] or "",
            tags=tags_list,
            base_path=str(r["base_path"]),
            visibility=str(r["visibility"]),
            backend_type=str(r["backend_type"]) if r["backend_type"] else "http",
            version=str(r["version"]) if r["version"] else "",
            updated_at=r["updated_at"].isoformat() if r["updated_at"] else "",
        ))
        if r["category"]:
            all_categories.add(str(r["category"]))
        for t in tags_list:
            all_tags.add(str(t))

    return PortalApiListResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        categories=sorted(all_categories),
        tags=sorted(all_tags),
    )


async def get_api_detail(api_id: str) -> PortalApiDetail:
    """取 API 详情（含全部版本列表）。"""
    async with db.db_session() as conn:
        api_row = await conn.fetchrow(
            "SELECT id, name, description, category, tags, base_path, visibility, status "
            "FROM api WHERE id = $1 AND status = 'published'",
            api_id,
        )
        if not api_row:
            raise ApiError(ErrorCode.NOT_FOUND, f"API {api_id} not found")

        ver_rows = await conn.fetch(
            """
            SELECT id, version, method, path, backend_type, status,
                   request_schema, response_schema, masking, ai_model, ai_streaming
            FROM api_version
            WHERE api_id = $1
            ORDER BY created_at DESC
            """,
            api_id,
        )

    raw_tags = api_row.get("tags") or []
    tags_list: list[str] = [str(t) for t in raw_tags] if isinstance(raw_tags, (list, tuple)) else []
    versions: list[PortalVersionItem] = []
    for vr in ver_rows:
        versions.append(PortalVersionItem(
            version_id=str(vr["id"]),
            version=str(vr["version"]),
            method=str(vr["method"]),
            path=str(vr["path"]),
            backend_type=str(vr["backend_type"]),
            status=str(vr["status"]),
            request_schema=vr["request_schema"],
            response_schema=vr["response_schema"],
            masking=vr["masking"],
            ai_model=vr["ai_model"],
            ai_streaming=bool(vr["ai_streaming"]),
        ))

    return PortalApiDetail(
        api_id=str(api_row["id"]),
        name=api_row["name"],
        description=api_row["description"],
        category=api_row["category"] or "",
        tags=tags_list,
        base_path=str(api_row["base_path"]),
        visibility=str(api_row["visibility"]),
        api_status=str(api_row["status"]),
        versions=versions,
    )


async def try_api(payload: TryRequest) -> TryResponse:
    """在线调试：用 API Key 调通后端真实 URL，返回响应 + 延迟。

    backend_url 从 PG 直接读取（不经过 PortalVersionItem，避免暴露给前端）。
    """
    import time

    # 1. 查 API + version 元数据（含 backend_url）
    async with db.db_session() as conn:
        api_row = await conn.fetchrow(
            "SELECT id, base_path FROM api WHERE id = $1 AND status = 'published'",
            payload.api_id,
        )
        if not api_row:
            return TryResponse(status=404, error=f"API {payload.api_id} not found")

        if payload.version_id:
            ver_row = await conn.fetchrow(
                """SELECT backend_type, backend_url, method
                   FROM api_version WHERE id = $1""",
                payload.version_id,
            )
        else:
            ver_row = await conn.fetchrow(
                """SELECT backend_type, backend_url, method
                   FROM api_version WHERE api_id = $1 AND status = 'published'
                   ORDER BY created_at DESC LIMIT 1""",
                payload.api_id,
            )
        if not ver_row:
            return TryResponse(status=404, error="No published version found")

    # 2. 验证 API Key → 调 auth-svc
    settings = get_settings()
    async with httpx.AsyncClient(timeout=5.0) as c:
        r = await c.post(
            settings.auth_service_url,
            json={"api_key": payload.api_key},
        )
    if r.status_code != 200:
        return TryResponse(status=401, error="API Key 无效")

    # 3. 拼 backend_url，替换路径参数
    backend_url = ver_row["backend_url"]
    for k, v in payload.path_params.items():
        backend_url = backend_url.replace(f"{{{k}}}", v)

    # 4. 构造请求
    headers = {"X-API-Key": payload.api_key, "Content-Type": "application/json"}
    headers.update(payload.headers)

    # 5. 执行请求
    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=payload.timeout_ms / 1000) as c:
            resp = await c.request(
                method=payload.method,
                url=backend_url,
                headers=headers,
                params=payload.query_params,
                json=payload.body if payload.body is not None else None,
            )
    except httpx.TimeoutException:
        elapsed = int((time.perf_counter() - start) * 1000)
        return TryResponse(status=504, error="后端响应超时", latency_ms=elapsed)
    except httpx.RequestError as e:
        elapsed = int((time.perf_counter() - start) * 1000)
        return TryResponse(status=502, error=f"后端不可达: {e}", latency_ms=elapsed)

    elapsed = int((time.perf_counter() - start) * 1000)

    # 6. 解析响应体
    ct = resp.headers.get("content-type", "")
    try:
        resp_body: Any = resp.json() if "json" in ct else resp.text[:4096]
    except Exception:
        resp_body = resp.text[:4096]

    return TryResponse(
        status=resp.status_code,
        headers={"content-type": ct},
        body=resp_body,
        latency_ms=elapsed,
    )


# ========== 用量/计费（Phase 3）==========

from portal.models import PlanInfo, SubscriptionInfo


async def get_billing_summary(tenant_id: str) -> dict:
    import httpx
    from apihub_core.config import get_settings
    settings = get_settings()
    from datetime import datetime
    month = datetime.utcnow().strftime("%Y-%m")
    quota_url = getattr(settings, "quota_service_url", "http://quota.apihub-system/v1/quota/billing")
    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.get(quota_url, params={"tenant_id": tenant_id, "month": month})
    if r.status_code != 200:
        return {"tenant_id": tenant_id, "month": month, "plan": {}, "daily_usage": [],
                "total_calls": 0, "total_tokens": 0, "remaining_calls_today": 0}
    return r.json()


async def list_plans() -> list[PlanInfo]:
    async with db.db_session() as conn:
        rows = await conn.fetch("SELECT * FROM plan WHERE status = 'active' ORDER BY sort_order")
    return [PlanInfo(code=r["code"], name=r["name"], description=r.get("description"),
                     price_cents=r["price_cents"], quota_included=r["quota_included"] or {},
                     rate_limits=r["rate_limits"] or {}, features=r.get("features"),
                     sort_order=r["sort_order"]) for r in rows]


async def get_subscription(tenant_id: str) -> SubscriptionInfo | None:
    async with db.db_session() as conn:
        row = await conn.fetchrow(
            "SELECT s.*, p.name AS plan_name FROM subscription s"
            " JOIN plan p ON p.code = s.plan_code"
            " WHERE s.tenant_id = $1 AND s.status = 'active' LIMIT 1",
            tenant_id,
        )
    if not row:
        return None
    return SubscriptionInfo(
        plan_code=row["plan_code"], plan_name=row["plan_name"],
        period_start=row["period_start"].isoformat(),
        period_end=row["period_end"].isoformat(),
        status=row["status"], auto_renew=row["auto_renew"],
    )
