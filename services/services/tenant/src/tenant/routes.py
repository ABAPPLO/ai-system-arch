"""tenant-svc 路由 —— 全部 /v1/tenant/* 前缀。

鉴权矩阵（docs/11 §6 + §9）：
  - 创建租户 / 列全部 / 删 / 改配额 : 仅超管
  - suspend / resume / close        : 仅超管
  - 单租户 GET / 成员 / 用量        : 超管 OR 该租户 owner/admin/developer/viewer
  - 加成员 / 改成员角色 / 删成员    : 超管 OR 该租户 owner/admin
  - 改 tenant name/slug/tier        : 仅超管

权限检查在 router 内做（不走 RLS，因为 tenant 表无 tenant_id 列）。
"""

from apihub_core.errors import ApiError, ErrorCode
from apihub_core.logging import get_logger
from apihub_core.tenant import require_tenant
from fastapi import FastAPI

from tenant import cache
from tenant import repository as repo
from tenant.models import (
    MemberAdd,
    MemberResponse,
    MemberUpdate,
    QuotaConfig,
    TenantCreate,
    TenantResponse,
    TenantUpdate,
    UsageResponse,
)

log = get_logger(__name__)


# ---------- 权限助手 ----------


def _require_platform_admin():
    """仅超管可调用。"""
    ctx = require_tenant()
    if not ctx.is_platform_admin:
        raise ApiError(
            ErrorCode.FORBIDDEN,
            "platform admin only",
        )
    return ctx


async def _require_tenant_role(tenant_id: str, min_roles: tuple[str, ...]):
    """超管 OR 该租户的特定角色。返回 ctx。"""
    ctx = require_tenant()
    if ctx.is_platform_admin:
        return ctx
    role = await repo.get_membership(tenant_id, ctx.user_id or "")
    if role is None or role not in min_roles:
        raise ApiError(
            ErrorCode.FORBIDDEN,
            f"requires role {min_roles} in tenant {tenant_id}",
        )
    return ctx


def _mask_name(name: str, is_admin: bool) -> str:
    """脱敏：普通用户看到部分隐藏（docs/11 §9.2）。"""
    if is_admin or len(name) <= 2:
        return name
    return name[:1] + "*" * (len(name) - 1)


def _to_response(row: dict, viewer_is_admin: bool) -> TenantResponse:
    """构造响应（普通用户 name 脱敏）。"""
    return TenantResponse(
        id=row["id"],
        name=_mask_name(row["name"], viewer_is_admin),
        slug=row["slug"],
        type=row["type"],
        status=row["status"],
        tier=row.get("tier", "standard"),
        parent_id=row.get("parent_id"),
        metadata=row.get("metadata") or {},
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ---------- 路由注册 ----------


def register_routes(app: FastAPI) -> None:
    # ========== 租户 CRUD ==========

    @app.post(
        "/v1/tenant/tenants",
        response_model=TenantResponse,
        status_code=201,
    )
    async def create_tenant(payload: TenantCreate):
        _require_platform_admin()
        row = await repo.create_tenant(payload)
        # 写缓存让上游服务立刻能读到
        await cache.set(row["id"], _cache_payload(row))
        log.info("tenant_created", tenant_id=row["id"], by="admin")
        return _to_response(row, viewer_is_admin=True)

    @app.get("/v1/tenant/tenants", response_model=list[TenantResponse])
    async def list_tenants(
        parent_id: str | None = None,
        type: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ):
        """超管：列出所有租户。普通用户：见 /v1/tenant/me。"""
        ctx = require_tenant()
        if ctx.is_platform_admin:
            rows = await repo.list_tenants(
                parent_id=parent_id,
                type_filter=type,
                status_filter=status,
                limit=limit,
                offset=offset,
            )
            return [_to_response(r, viewer_is_admin=True) for r in rows]

        # 普通用户：只能看自己加入的租户
        if not ctx.user_id:
            raise ApiError(ErrorCode.FORBIDDEN, "no user context")
        rows = await repo.get_user_tenants(ctx.user_id)
        return [_to_response(r, viewer_is_admin=False) for r in rows]

    @app.get("/v1/tenant/tenants/me", response_model=list[TenantResponse])
    async def list_my_tenants():
        """当前用户加入的所有租户（前端切换用）。"""
        ctx = require_tenant()
        if not ctx.user_id:
            raise ApiError(ErrorCode.FORBIDDEN, "no user context")
        rows = await repo.get_user_tenants(ctx.user_id)
        is_admin = ctx.is_platform_admin
        return [_to_response(r, viewer_is_admin=is_admin) for r in rows]

    @app.get("/v1/tenant/tenants/{tenant_id}", response_model=TenantResponse)
    async def get_tenant(tenant_id: str):
        ctx = await _require_tenant_role(
            tenant_id, min_roles=("owner", "admin", "developer", "viewer")
        )
        row = await repo.get_tenant(tenant_id)
        return _to_response(row, viewer_is_admin=ctx.is_platform_admin)

    @app.put("/v1/tenant/tenants/{tenant_id}", response_model=TenantResponse)
    async def update_tenant(tenant_id: str, payload: TenantUpdate):
        _require_platform_admin()
        row = await repo.update_tenant(tenant_id, payload)
        await cache.set(row["id"], _cache_payload(row))
        return _to_response(row, viewer_is_admin=True)

    # ========== 状态机 ==========

    @app.post("/v1/tenant/tenants/{tenant_id}/suspend", response_model=TenantResponse)
    async def suspend(tenant_id: str):
        _require_platform_admin()
        row = await repo.change_status(tenant_id, "suspended")
        await cache.invalidate(tenant_id)  # 让上游重新读 PG 拒绝
        log.info("tenant_suspended", tenant_id=tenant_id)
        return _to_response(row, viewer_is_admin=True)

    @app.post("/v1/tenant/tenants/{tenant_id}/resume", response_model=TenantResponse)
    async def resume(tenant_id: str):
        _require_platform_admin()
        row = await repo.change_status(tenant_id, "active")
        await cache.set(tenant_id, _cache_payload(row))
        log.info("tenant_resumed", tenant_id=tenant_id)
        return _to_response(row, viewer_is_admin=True)

    @app.post("/v1/tenant/tenants/{tenant_id}/close", response_model=TenantResponse)
    async def close(tenant_id: str):
        _require_platform_admin()
        row = await repo.change_status(tenant_id, "closed")
        # 关闭：失效缓存 + 失效该租户所有 APIKey（auth 服务读不到 meta 就拒绝）
        await cache.invalidate(tenant_id)
        log.info("tenant_closed", tenant_id=tenant_id)
        return _to_response(row, viewer_is_admin=True)

    # ========== 成员 ==========

    @app.get(
        "/v1/tenant/tenants/{tenant_id}/members",
        response_model=list[MemberResponse],
    )
    async def list_members(tenant_id: str):
        await _require_tenant_role(tenant_id, min_roles=("owner", "admin", "developer", "viewer"))
        rows = await repo.list_members(tenant_id)
        return [MemberResponse(**r) for r in rows]

    @app.post(
        "/v1/tenant/tenants/{tenant_id}/members",
        response_model=MemberResponse,
        status_code=201,
    )
    async def add_member(tenant_id: str, payload: MemberAdd):
        await _require_tenant_role(tenant_id, min_roles=("owner", "admin"))
        row = await repo.add_member(tenant_id, payload.user_id, payload.role)
        log.info(
            "member_added",
            tenant_id=tenant_id,
            user_id=payload.user_id,
            role=payload.role,
        )
        return MemberResponse(**row)

    @app.put(
        "/v1/tenant/tenants/{tenant_id}/members/{user_id}",
        response_model=MemberResponse,
    )
    async def update_member(tenant_id: str, user_id: str, payload: MemberUpdate):
        await _require_tenant_role(tenant_id, min_roles=("owner", "admin"))
        row = await repo.update_member_role(tenant_id, user_id, payload.role)
        return MemberResponse(**row)

    @app.delete("/v1/tenant/tenants/{tenant_id}/members/{user_id}", status_code=204)
    async def remove_member(tenant_id: str, user_id: str):
        await _require_tenant_role(tenant_id, min_roles=("owner", "admin"))
        await repo.remove_member(tenant_id, user_id)
        log.info("member_removed", tenant_id=tenant_id, user_id=user_id)

    # ========== 配额 ==========

    @app.get("/v1/tenant/tenants/{tenant_id}/quota", response_model=QuotaConfig)
    async def get_quota(tenant_id: str):
        await _require_tenant_role(tenant_id, min_roles=("owner", "admin", "developer", "viewer"))
        quota = await repo.get_quota(tenant_id)
        return QuotaConfig(**_normalize_quota(quota))

    @app.put("/v1/tenant/tenants/{tenant_id}/quota", response_model=QuotaConfig)
    async def set_quota(tenant_id: str, payload: QuotaConfig):
        _require_platform_admin()
        row = await repo.set_quota(tenant_id, payload.model_dump(mode="json"))
        # 配额变更 → 失效缓存（quota 服务下次 load_rules 会重读）
        await cache.invalidate(tenant_id)
        log.info("quota_updated", tenant_id=tenant_id)
        return QuotaConfig(**_normalize_quota(row["metadata"].get("quota") or {}))

    # ========== 用量 ==========

    @app.get("/v1/tenant/tenants/{tenant_id}/usage", response_model=UsageResponse)
    async def get_usage(tenant_id: str):
        """当日用量（Phase 1 占位 —— Phase 3 调 quota/analyzer 聚合）。"""
        await _require_tenant_role(tenant_id, min_roles=("owner", "admin", "developer", "viewer"))
        quota = await repo.get_quota(tenant_id)
        day_limit = int((quota or {}).get("day_limit", 0))
        return UsageResponse(
            tenant_id=tenant_id,
            day_used=0,  # TODO Phase 3: 调 quota /usage 聚合
            day_limit=day_limit,
            remaining=day_limit,
        )

    # ========== 子租户 ==========

    @app.get(
        "/v1/tenant/tenants/{tenant_id}/children",
        response_model=list[TenantResponse],
    )
    async def list_children(tenant_id: str):
        ctx = await _require_tenant_role(
            tenant_id, min_roles=("owner", "admin", "developer", "viewer")
        )
        rows = await repo.list_children(tenant_id)
        return [_to_response(r, viewer_is_admin=ctx.is_platform_admin) for r in rows]

    # ========== 健康 ==========

    @app.get("/v1/tenant/health")
    async def health():
        return {"status": "ok", "service": "tenant"}


# ---------- 工具 ----------


def _cache_payload(row: dict) -> dict:
    """构造写缓存的精简 payload（auth/quota 关心的字段）。"""
    return {
        "id": row["id"],
        "name": row["name"],
        "type": row["type"],
        "status": row["status"],
        "tier": row.get("tier", "standard"),
        "metadata": row.get("metadata") or {},
    }


def _normalize_quota(raw: dict | None) -> dict:
    """补全缺失字段。"""
    raw = raw or {}
    return {
        "day_limit": int(raw.get("day_limit", 0)),
        "rate_limit": raw.get("rate_limit") or {},
    }
