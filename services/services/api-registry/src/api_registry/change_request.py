"""接口变更评审工单 —— api_change_request 表的 CRUD + 状态机。

状态机（ADR-005 分级审批）：
    pending ──approved──→ applied
        │                  ↑
        │                  │
        │              (apply 时执行实际操作：发布 / 下线)
        │
        ├──rejected──→ rejected
        │
        └──cancelled (提交方撤回)

env 分级（target_env）：
    dev      → 自助（pending → 自动 applied，无 review）
    staging  → 简单审批（一次 review）
    prod     → 强审批（钉钉审批单 + 平台运维 review）

钉钉集成：submit 时调钉钉审批流（stub），dingtalk_approval_id 回填。
钉钉审批通过 → webhook 回调 → 自动 approve + apply。
"""

from datetime import UTC, datetime
from enum import StrEnum

import asyncpg
from apihub_core import db
from pydantic import BaseModel, ConfigDict, Field

from api_registry.models import ApiStatus

# ============ Enums ============

class ChangeType(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    PUBLISH = "publish"
    DEPRECATE = "deprecate"
    RETIRE = "retire"


class ChangeRequestStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    APPLIED = "applied"
    CANCELLED = "cancelled"


class TargetEnv(StrEnum):
    DEV = "dev"
    STAGING = "staging"
    PROD = "prod"


# ============ Pydantic 模型 ============


class ChangeRequestCreate(BaseModel):
    model_config = ConfigDict(coerce_numbers_to_str=True)

    api_id: str
    target_version: str = Field(pattern=r"^v\d+$")
    change_type: ChangeType
    target_env: TargetEnv
    proposed_config: dict
    submitted_by: str = Field(min_length=1, max_length=64)


class ChangeRequestReview(BaseModel):
    """审批 / 退回 / 撤回的请求体。"""
    review_comment: str | None = Field(default=None, max_length=2000)


class ChangeRequest(BaseModel):
    model_config = ConfigDict(coerce_numbers_to_str=True)

    id: int
    tenant_id: str
    api_id: str
    target_version: str
    change_type: ChangeType
    target_env: TargetEnv
    proposed_config: dict
    current_config: dict | None = None
    diff_summary: str | None = None
    status: ChangeRequestStatus
    dingtalk_approval_id: str | None = None
    submitted_by: str
    submitted_at: datetime
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    review_comment: str | None = None
    applied_at: datetime | None = None


class ListChangeRequestsQuery(BaseModel):
    model_config = ConfigDict(coerce_numbers_to_str=True)

    api_id: str | None = None
    status: ChangeRequestStatus | None = None
    change_type: ChangeType | None = None
    target_env: TargetEnv | None = None
    submitted_by: str | None = None
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


# ============ Repository ============

async def submit_change_request(
    *,
    tenant_id: str,
    req: ChangeRequestCreate,
    diff_summary: str | None = None,
    current_config: dict | None = None,
    dingtalk_approval_id: str | None = None,
) -> int:
    """INSERT 新 change_request。dev 环境 status 自动 = approved（自助）。"""
    initial_status = (
        ChangeRequestStatus.APPROVED if req.target_env == TargetEnv.DEV
        else ChangeRequestStatus.PENDING
    )
    async with db.db_session() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO api_change_request (
                tenant_id, api_id, target_version, change_type, target_env,
                proposed_config, current_config, diff_summary,
                status, dingtalk_approval_id, submitted_by
            ) VALUES (
                $1, $2, $3, $4, $5,
                $6, $7, $8,
                $9, $10, $11
            )
            RETURNING id
            """,
            tenant_id,
            req.api_id,
            req.target_version,
            req.change_type.value,
            req.target_env.value,
            req.proposed_config,
            current_config,
            diff_summary,
            initial_status.value,
            dingtalk_approval_id,
            req.submitted_by,
        )
    return int(row["id"])


async def get_change_request(request_id: int) -> ChangeRequest | None:
    async with db.db_session() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, tenant_id, api_id, target_version, change_type, target_env,
                   proposed_config, current_config, diff_summary,
                   status, dingtalk_approval_id,
                   submitted_by, submitted_at,
                   reviewed_by, reviewed_at, review_comment,
                   applied_at
            FROM api_change_request
            WHERE id = $1
            """,
            request_id,
        )
    return _row_to_request(row) if row else None


async def list_change_requests(query: ListChangeRequestsQuery) -> list[ChangeRequest]:
    clauses: list[str] = []
    params: list = []
    idx = 1

    if query.api_id is not None:
        clauses.append(f"api_id = ${idx}")
        params.append(query.api_id)
        idx += 1
    if query.status is not None:
        clauses.append(f"status = ${idx}")
        params.append(query.status.value)
        idx += 1
    if query.change_type is not None:
        clauses.append(f"change_type = ${idx}")
        params.append(query.change_type.value)
        idx += 1
    if query.target_env is not None:
        clauses.append(f"target_env = ${idx}")
        params.append(query.target_env.value)
        idx += 1
    if query.submitted_by is not None:
        clauses.append(f"submitted_by = ${idx}")
        params.append(query.submitted_by)
        idx += 1

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"""
        SELECT id, tenant_id, api_id, target_version, change_type, target_env,
               proposed_config, current_config, diff_summary,
               status, dingtalk_approval_id,
               submitted_by, submitted_at,
               reviewed_by, reviewed_at, review_comment,
               applied_at
        FROM api_change_request
        {where}
        ORDER BY submitted_at DESC, id DESC
        LIMIT ${idx} OFFSET ${idx + 1}
    """  # noqa: S608
    params.extend([query.limit, query.offset])

    async with db.db_session() as conn:
        rows = await conn.fetch(sql, *params)
    return [_row_to_request(r) for r in rows]


async def approve_change_request(
    request_id: int, *, reviewed_by: str, review_comment: str | None = None
) -> bool:
    """pending → approved。False = 行不存在 / 状态不对。"""
    async with db.db_session() as conn:
        result = await conn.execute(
            """
            UPDATE api_change_request
            SET status = 'approved', reviewed_by = $2, reviewed_at = NOW(),
                review_comment = $3
            WHERE id = $1 AND status = 'pending'
            """,
            request_id,
            reviewed_by,
            review_comment,
        )
    return result.endswith(" 1")


async def reject_change_request(
    request_id: int, *, reviewed_by: str, review_comment: str | None = None
) -> bool:
    """pending → rejected。"""
    async with db.db_session() as conn:
        result = await conn.execute(
            """
            UPDATE api_change_request
            SET status = 'rejected', reviewed_by = $2, reviewed_at = NOW(),
                review_comment = $3
            WHERE id = $1 AND status = 'pending'
            """,
            request_id,
            reviewed_by,
            review_comment,
        )
    return result.endswith(" 1")


async def cancel_change_request(
    request_id: int, *, submitted_by: str
) -> bool:
    """提交方撤回（仅 pending → cancelled）。"""
    async with db.db_session() as conn:
        result = await conn.execute(
            """
            UPDATE api_change_request
            SET status = 'cancelled'
            WHERE id = $1 AND status = 'pending' AND submitted_by = $2
            """,
            request_id,
            submitted_by,
        )
    return result.endswith(" 1")


async def mark_applied(request_id: int) -> bool:
    """approved → applied。"""
    async with db.db_session() as conn:
        result = await conn.execute(
            """
            UPDATE api_change_request
            SET status = 'applied', applied_at = NOW()
            WHERE id = $1 AND status = 'approved'
            """,
            request_id,
        )
    return result.endswith(" 1")


# ============ 钉钉审批 stub ============

async def submit_dingtalk_approval(req: ChangeRequestCreate) -> str | None:
    """提交钉钉审批单，返回 approval_id。

    stub 实现：dev 环境返回 None（不需要钉钉）；staging/prod 返回模拟 ID。
    真实环境用钉钉开放平台 API：
      POST /v1.0/workflow/processes/create
      需配置 process_code 模板（详见 docs/09-deployment.md §10.3）。
    """
    if req.target_env == TargetEnv.DEV:
        return None
    # stub：返回伪造 ID，真实环境替换为钉钉 API 调用
    return f"dt_{req.target_env.value}_{req.api_id}_{int(datetime.now(UTC).timestamp())}"


# ============ Helpers ============

def _row_to_request(row: asyncpg.Record) -> ChangeRequest:
    proposed = row["proposed_config"]
    current = row["current_config"]
    return ChangeRequest(
        id=int(row["id"]),
        tenant_id=row["tenant_id"],
        api_id=row["api_id"],
        target_version=row["target_version"],
        change_type=ChangeType(row["change_type"]),
        target_env=TargetEnv(row["target_env"]),
        proposed_config=proposed if proposed else {},
        current_config=current if current else None,
        diff_summary=row["diff_summary"],
        status=ChangeRequestStatus(row["status"]),
        dingtalk_approval_id=row["dingtalk_approval_id"],
        submitted_by=row["submitted_by"],
        submitted_at=row["submitted_at"],
        reviewed_by=row["reviewed_by"],
        reviewed_at=row["reviewed_at"],
        review_comment=row["review_comment"],
        applied_at=row["applied_at"],
    )


# ============ Apply hook（实际生效）============

async def apply_change(req: ChangeRequest) -> str:
    """approved 的 change_request 真正落地。

    根据 change_type 触发不同副作用：
      - publish  → api_version.status = published
      - deprecate → api_version.status = deprecated
      - retire   → api_version.status = retired
      - create / update → 仅更新 proposed_config（用户自己负责细节）

    返回 apply_summary（用于审计）。
    """
    summary_parts = [f"change_type={req.change_type.value}"]

    if req.change_type in (
        ChangeType.PUBLISH, ChangeType.DEPRECATE, ChangeType.RETIRE,
    ):
        target_status = {
            ChangeType.PUBLISH: ApiStatus.PUBLISHED,
            ChangeType.DEPRECATE: ApiStatus.DEPRECATED,
            ChangeType.RETIRE: ApiStatus.RETIRED,
        }[req.change_type]

        async with db.db_session() as conn:
            result = await conn.execute(
                """
                UPDATE api_version
                SET status = $2,
                    published_at = CASE WHEN $2 = 'published' THEN NOW() ELSE published_at END,
                    deprecated_at = CASE WHEN $2 = 'deprecated' THEN NOW() ELSE deprecated_at END,
                    retired_at = CASE WHEN $2 = 'retired' THEN NOW() ELSE retired_at END
                WHERE api_id = $1 AND version = $3
                """,
                req.api_id,
                target_status.value,
                req.target_version,
            )
        if not result.endswith(" 1"):
            raise RuntimeError(
                f"apply failed: api_version (api_id={req.api_id}, "
                f"version={req.target_version}) not found"
            )
        summary_parts.append(f"api_version.status → {target_status.value}")

    await mark_applied(req.id)
    return "; ".join(summary_parts)
