"""DB 访问层 —— 全部走 admin_db_session。

理由（schema 02-init §1 注释 + docs/11 §4.2）：
  tenant / tenant_member 表本身无 tenant_id 列（前者就是租户元数据，
  后者是用户-租户多对多），RLS 没法挂。隔离由应用层 + 角色检查保证：
    - 创建/暂停/关闭/改配额 → 必须超管
    - 加成员/改成员角色 → 必须该租户的 owner/admin（先验 membership）
"""

from typing import Any

from apihub_core import db
from apihub_core.errors import ApiError, ErrorCode

from tenant.models import (
    VALID_ROLES,
    VALID_STATUSES,
    VALID_TIERS,
    VALID_TYPES,
    TenantCreate,
    TenantUpdate,
)

# ---------- 租户 CRUD ----------


async def create_tenant(payload: TenantCreate) -> dict[str, Any]:
    """创建租户。冲突 id/slug → 409。"""
    ttype = payload.type if payload.type in VALID_TYPES else "internal"
    tier = payload.tier if payload.tier in VALID_TIERS else "standard"

    async with db.admin_db_session() as conn:
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO tenant (id, parent_id, name, slug, type, status, tier, metadata)
                VALUES ($1, $2, $3, $4, $5, 'active', $6, $7::jsonb)
                RETURNING id, parent_id, name, slug, type, status, tier, metadata, created_at, updated_at
                """,
                payload.id,
                payload.parent_id,
                payload.name,
                payload.slug,
                ttype,
                tier,
                jsonb(payload.metadata),
            )
        except Exception as e:
            msg = str(e)
            if "primary key" in msg or replay_unique(msg, payload.id):
                raise ApiError(
                    ErrorCode.CONFLICT, f"tenant {payload.id} already exists"
                ) from e
            if "slug" in msg and "unique" in msg.lower():
                raise ApiError(
                    ErrorCode.CONFLICT, f"slug '{payload.slug}' already taken"
                ) from e
            raise

    return dict(row)


async def get_tenant(tenant_id: str) -> dict[str, Any]:
    """单租户查询。不存在 → 404。"""
    async with db.admin_db_session() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, parent_id, name, slug, type, status, tier, metadata, created_at, updated_at
            FROM tenant WHERE id = $1
            """,
            tenant_id,
        )
    if not row:
        raise ApiError(ErrorCode.NOT_FOUND, f"tenant {tenant_id} not found")
    return dict(row)


async def list_tenants(
    *,
    parent_id: str | None = None,
    type_filter: str | None = None,
    status_filter: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """列表（超管用）。支持父/type/status 过滤。"""
    clauses = []
    params: list[Any] = []
    if parent_id is not None:
        params.append(parent_id)
        clauses.append(f"parent_id = ${len(params)}")
    if type_filter is not None:
        params.append(type_filter)
        clauses.append(f"type = ${len(params)}")
    if status_filter is not None:
        params.append(status_filter)
        clauses.append(f"status = ${len(params)}")

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.extend([limit, offset])

    async with db.admin_db_session() as conn:
        rows = await conn.fetch(
            f"""
            SELECT id, parent_id, name, slug, type, status, tier, metadata, created_at, updated_at
            FROM tenant
            {where}
            ORDER BY created_at DESC
            LIMIT ${len(params) - 1} OFFSET ${len(params)}
            """,  # noqa: S608 - fields/where are controlled internally, params are bound
            *params,
        )
    return [dict(r) for r in rows]


async def list_children(parent_id: str) -> list[dict[str, Any]]:
    """直接子租户列表。"""
    async with db.admin_db_session() as conn:
        rows = await conn.fetch(
            """
            SELECT id, parent_id, name, slug, type, status, tier, metadata, created_at, updated_at
            FROM tenant WHERE parent_id = $1 ORDER BY created_at DESC
            """,
            parent_id,
        )
    return [dict(r) for r in rows]


async def update_tenant(tenant_id: str, payload: TenantUpdate) -> dict[str, Any]:
    """更新 name/slug/tier/metadata。RETURNING 新值。"""
    fields: list[str] = []
    params: list[Any] = []
    if payload.name is not None:
        params.append(payload.name)
        fields.append(f"name = ${len(params)}")
    if payload.slug is not None:
        params.append(payload.slug)
        fields.append(f"slug = ${len(params)}")
    if payload.tier is not None:
        if payload.tier not in VALID_TIERS:
            raise ApiError(ErrorCode.INVALID_PARAMS, f"bad tier {payload.tier}")
        params.append(payload.tier)
        fields.append(f"tier = ${len(params)}")
    if payload.metadata is not None:
        params.append(jsonb(payload.metadata))
        fields.append(f"metadata = ${len(params)}")

    if not fields:
        return await get_tenant(tenant_id)

    params.append(tenant_id)
    async with db.admin_db_session() as conn:
        try:
            row = await conn.fetchrow(
                f"""
                UPDATE tenant SET {', '.join(fields)}, updated_at = NOW()
                WHERE id = ${len(params)}
                RETURNING id, parent_id, name, slug, type, status, tier, metadata, created_at, updated_at
                """,  # noqa: S608 - fields are controlled internally, params are bound
                *params,
            )
        except Exception as e:
            if "slug" in str(e) and "unique" in str(e).lower():
                raise ApiError(
                    ErrorCode.CONFLICT, f"slug '{payload.slug}' already taken"
                ) from e
            raise

    if not row:
        raise ApiError(ErrorCode.NOT_FOUND, f"tenant {tenant_id} not found")
    return dict(row)


# ---------- 状态机 ----------


async def change_status(tenant_id: str, new_status: str) -> dict[str, Any]:
    """状态机：active ↔ suspended；active/suspended → closed（终态）。

    非法转换 → 409 CONFLICT。
    """
    if new_status not in VALID_STATUSES:
        raise ApiError(ErrorCode.INVALID_PARAMS, f"bad status {new_status}")

    current = await get_tenant(tenant_id)
    cur_status = current["status"]

    if cur_status == new_status:
        return current  # 幂等

    if cur_status == "closed":
        raise ApiError(
            ErrorCode.CONFLICT,
            f"tenant {tenant_id} is closed (terminal state)",
        )

    if new_status == "closed":
        allowed_from = ("active", "suspended")
    elif new_status == "suspended":
        allowed_from = ("active",)
    elif new_status == "active":
        allowed_from = ("suspended",)  # resume
    else:
        allowed_from = ()

    if cur_status not in allowed_from:
        raise ApiError(
            ErrorCode.CONFLICT,
            f"cannot transition {cur_status} → {new_status}",
        )

    async with db.admin_db_session() as conn:
        row = await conn.fetchrow(
            """
            UPDATE tenant SET status = $1, updated_at = NOW()
            WHERE id = $2 AND status = ANY($3::text[])
            RETURNING id, parent_id, name, slug, type, status, tier, metadata, created_at, updated_at
            """,
            new_status,
            tenant_id,
            allowed_from,
        )

    if not row:
        # 并发改了，重新读
        return await get_tenant(tenant_id)
    return dict(row)


# ---------- 成员 ----------


async def list_members(tenant_id: str) -> list[dict[str, Any]]:
    """租户下所有成员。"""
    await _ensure_tenant_exists(tenant_id)
    async with db.admin_db_session() as conn:
        rows = await conn.fetch(
            """
            SELECT id, tenant_id, user_id, role, created_at
            FROM tenant_member WHERE tenant_id = $1 ORDER BY created_at DESC
            """,
            tenant_id,
        )
    return [dict(r) for r in rows]


async def add_member(tenant_id: str, user_id: str, role: str) -> dict[str, Any]:
    """加成员。重复 → 409。"""
    if role not in VALID_ROLES:
        raise ApiError(ErrorCode.INVALID_PARAMS, f"bad role {role}")
    await _ensure_tenant_exists(tenant_id)

    member_id = f"tm_{tenant_id}_{user_id}"
    async with db.admin_db_session() as conn:
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO tenant_member (id, tenant_id, user_id, role)
                VALUES ($1, $2, $3, $4)
                RETURNING id, tenant_id, user_id, role, created_at
                """,
                member_id,
                tenant_id,
                user_id,
                role,
            )
        except Exception as e:
            if "unique" in str(e).lower() or "duplicate" in str(e).lower():
                raise ApiError(
                    ErrorCode.CONFLICT,
                    f"user {user_id} already member of {tenant_id}",
                ) from e
            raise

    return dict(row)


async def update_member_role(
    tenant_id: str, user_id: str, role: str
) -> dict[str, Any]:
    """改成员角色。"""
    if role not in VALID_ROLES:
        raise ApiError(ErrorCode.INVALID_PARAMS, f"bad role {role}")

    async with db.admin_db_session() as conn:
        row = await conn.fetchrow(
            """
            UPDATE tenant_member SET role = $1
            WHERE tenant_id = $2 AND user_id = $3
            RETURNING id, tenant_id, user_id, role, created_at
            """,
            role,
            tenant_id,
            user_id,
        )
    if not row:
        raise ApiError(
            ErrorCode.NOT_FOUND,
            f"user {user_id} not in tenant {tenant_id}",
        )
    return dict(row)


async def remove_member(tenant_id: str, user_id: str) -> None:
    """移除成员。不存在 → 404。"""
    async with db.admin_db_session() as conn:
        result = await conn.execute(
            "DELETE FROM tenant_member WHERE tenant_id = $1 AND user_id = $2",
            tenant_id,
            user_id,
        )
    if result == "DELETE 0":
        raise ApiError(
            ErrorCode.NOT_FOUND,
            f"user {user_id} not in tenant {tenant_id}",
        )


async def get_user_tenants(user_id: str) -> list[dict[str, Any]]:
    """用户加入的所有租户（普通用户列表端点用）。"""
    async with db.admin_db_session() as conn:
        rows = await conn.fetch(
            """
            SELECT t.id, t.parent_id, t.name, t.slug, t.type, t.status, t.tier,
                   t.metadata, t.created_at, t.updated_at, tm.role
            FROM tenant_member tm
            JOIN tenant t ON t.id = tm.tenant_id
            WHERE tm.user_id = $1
            ORDER BY tm.created_at DESC
            """,
            user_id,
        )
    return [dict(r) for r in rows]


async def get_membership(tenant_id: str, user_id: str) -> str | None:
    """返回 role 或 None（用于权限校验）。"""
    async with db.admin_db_session() as conn:
        row = await conn.fetchrow(
            "SELECT role FROM tenant_member WHERE tenant_id = $1 AND user_id = $2",
            tenant_id,
            user_id,
        )
    return row["role"] if row else None


# ---------- 配额（写入 tenant.metadata.quota） ----------


async def get_quota(tenant_id: str) -> dict[str, Any]:
    """读 metadata.quota 子字段。"""
    tenant = await get_tenant(tenant_id)
    meta = tenant.get("metadata") or {}
    return meta.get("quota") or {"day_limit": 0, "rate_limit": {}}


async def set_quota(tenant_id: str, quota: dict[str, Any]) -> dict[str, Any]:
    """覆写 metadata.quota（保留其他 metadata 字段）。"""
    async with db.admin_db_session() as conn:
        row = await conn.fetchrow(
            """
            UPDATE tenant
            SET metadata = jsonb_set(
                    COALESCE(metadata, '{}'::jsonb),
                    '{quota}',
                    $2::jsonb,
                    true
                ),
                updated_at = NOW()
            WHERE id = $1
            RETURNING id, parent_id, name, slug, type, status, tier, metadata, created_at, updated_at
            """,
            tenant_id,
            jsonb(quota),
        )
    if not row:
        raise ApiError(ErrorCode.NOT_FOUND, f"tenant {tenant_id} not found")
    return dict(row)


# ---------- 内部工具 ----------


async def _ensure_tenant_exists(tenant_id: str) -> None:
    """快速存在性检查（轻量 SELECT 1）。"""
    async with db.admin_db_session() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM tenant WHERE id = $1", tenant_id
        )
    if not row:
        raise ApiError(ErrorCode.NOT_FOUND, f"tenant {tenant_id} not found")


def jsonb(data: Any) -> str:
    """asyncpg 接受 str，自己序列化避免类型推断问题。"""
    import json

    return json.dumps(data, default=str)


def replay_unique(msg: str, key: str) -> bool:
    """asyncpg 抛 unique violation 时 msg 形如
    'duplicate key value violates unique constraint "tenant_pkey"'。"""
    return "unique" in msg.lower() and key in msg
