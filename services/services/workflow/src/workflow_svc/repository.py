"""workflow_instance 表的 PG 操作。

表结构（scripts/init-db/04-phase3.sql 建表）：

    CREATE TABLE workflow_instance (
        tenant_id       text NOT NULL,
        id              bigserial PRIMARY KEY,
        workflow_uuid   VARCHAR(64) NOT NULL UNIQUE,
        argo_name       VARCHAR(128) NOT NULL,
        namespace       VARCHAR(64) NOT NULL DEFAULT 'apihub-workflows',
        api_id          text,
        app_id          text,
        trace_id        VARCHAR(64),
        spec            JSONB NOT NULL,
        status          VARCHAR(20) NOT NULL DEFAULT 'submitted',
        message         TEXT,
        submitted_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        finished_at     TIMESTAMPTZ,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    -- tenant_id / api_id / app_id 全部 text，与 Phase 1 约定一致（参见 03-phase2.sql）
    -- id 是 bigserial（自增 int），PG 内部 bigint；返回 Python int。

所有读操作走 db_session（自动 RLS）；写操作（submit/cancel/resume 的回调）
也走 db_session 因为是 HTTP 上下文触发。
"""

from datetime import datetime

import asyncpg
from apihub_core import db

# apihub_core.db 已为 jsonb 注册 codec（encoder=json.dumps / decoder=json.loads），
# 所以 jsonb 列在 Python 侧直接是 dict —— spec 写入直传 dict，读取直读列，
# 既不要 json.dumps（双重编码），也不要 `::text` + json.loads。
from workflow_svc.models import (
    ListWorkflowsQuery,
    WorkflowDetail,
    WorkflowListItem,
    WorkflowStatus,
)

# ============ 写操作 ============


async def create_workflow(
    *,
    tenant_id: str,
    workflow_uuid: str,
    argo_name: str,
    namespace: str,
    api_id: str,
    app_id: str,
    trace_id: str,
    spec: dict,
    status: WorkflowStatus = WorkflowStatus.SUBMITTED,
) -> int:
    """INSERT 新 workflow_instance，返回 PG id。"""
    async with db.db_session() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO workflow_instance (
                tenant_id, workflow_uuid, argo_name, namespace,
                api_id, app_id, trace_id, spec, status,
                submitted_at
            ) VALUES (
                $1, $2, $3, $4,
                $5, $6, $7, $8, $9,
                NOW()
            )
            RETURNING id
            """,
            tenant_id,
            workflow_uuid,
            argo_name,
            namespace,
            api_id,
            app_id,
            trace_id,
            spec,
            status.value,
        )
    return int(row["id"])


async def update_status(
    workflow_id: int,
    *,
    status: WorkflowStatus,
    message: str | None = None,
    finished_at: datetime | None = None,
) -> bool:
    """更新 workflow 状态。False = 行不存在 / 状态未变。"""
    async with db.db_session() as conn:
        if finished_at is not None:
            result = await conn.execute(
                """
                UPDATE workflow_instance
                SET status = $2, message = $3, finished_at = $4, updated_at = NOW()
                WHERE id = $1
                """,
                workflow_id,
                status.value,
                message,
                finished_at,
            )
        else:
            result = await conn.execute(
                """
                UPDATE workflow_instance
                SET status = $2, message = $3, updated_at = NOW()
                WHERE id = $1
                """,
                workflow_id,
                status.value,
                message,
            )
    return result.endswith(" 1")


# ============ 读操作 ============


async def get_workflow(workflow_id: int) -> WorkflowDetail | None:
    async with db.db_session() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, tenant_id, workflow_uuid, argo_name, namespace,
                   api_id, app_id, trace_id, spec,
                   status, message, submitted_at, finished_at,
                   created_at, updated_at
            FROM workflow_instance
            WHERE id = $1
            """,
            workflow_id,
        )
    if row is None:
        return None
    return _row_to_detail(row)


async def get_workflow_by_uuid(workflow_uuid: str) -> WorkflowDetail | None:
    async with db.db_session() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, tenant_id, workflow_uuid, argo_name, namespace,
                   api_id, app_id, trace_id, spec,
                   status, message, submitted_at, finished_at,
                   created_at, updated_at
            FROM workflow_instance
            WHERE workflow_uuid = $1
            """,
            workflow_uuid,
        )
    if row is None:
        return None
    return _row_to_detail(row)


async def list_workflows(query: ListWorkflowsQuery) -> list[WorkflowListItem]:
    """列表查询，RLS 自动过滤。"""
    clauses: list[str] = []
    params: list = []
    idx = 1

    if query.api_id is not None:
        clauses.append(f"api_id = ${idx}")
        params.append(query.api_id)
        idx += 1
    if query.app_id is not None:
        clauses.append(f"app_id = ${idx}")
        params.append(query.app_id)
        idx += 1
    if query.trace_id is not None:
        clauses.append(f"trace_id = ${idx}")
        params.append(query.trace_id)
        idx += 1
    if query.status is not None:
        clauses.append(f"status = ${idx}")
        params.append(query.status.value)
        idx += 1
    if query.since is not None:
        clauses.append(f"submitted_at >= ${idx}")
        params.append(query.since)
        idx += 1
    if query.until is not None:
        clauses.append(f"submitted_at <= ${idx}")
        params.append(query.until)
        idx += 1

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"""
        SELECT id, tenant_id, workflow_uuid, argo_name,
               api_id, app_id, trace_id,
               status, submitted_at, finished_at
        FROM workflow_instance
        {where}
        ORDER BY submitted_at DESC, id DESC
        LIMIT ${idx} OFFSET ${idx + 1}
    """  # noqa: S608
    params.extend([query.limit, query.offset])

    async with db.db_session() as conn:
        rows = await conn.fetch(sql, *params)
    return [_row_to_list_item(r) for r in rows]


# ============ 辅助函数 ============


def _row_to_detail(row: asyncpg.Record) -> WorkflowDetail:
    spec_raw = row["spec"]
    return WorkflowDetail(
        id=int(row["id"]),
        tenant_id=row["tenant_id"],
        workflow_uuid=row["workflow_uuid"],
        argo_name=row["argo_name"],
        namespace=row["namespace"],
        api_id=row["api_id"] or "",
        app_id=row["app_id"] or "",
        trace_id=row["trace_id"] or "",
        spec=spec_raw if isinstance(spec_raw, dict) else {},
        status=WorkflowStatus(row["status"]),
        submitted_at=row["submitted_at"],
        finished_at=row["finished_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        message=row["message"],
        steps=[],  # 由 routes.py 调 argo_client.get_steps 填
    )


def _row_to_list_item(row: asyncpg.Record) -> WorkflowListItem:
    return WorkflowListItem(
        id=int(row["id"]),
        tenant_id=row["tenant_id"],
        workflow_uuid=row["workflow_uuid"],
        argo_name=row["argo_name"],
        api_id=row["api_id"] or "",
        app_id=row["app_id"] or "",
        trace_id=row["trace_id"] or "",
        status=WorkflowStatus(row["status"]),
        submitted_at=row["submitted_at"],
        finished_at=row["finished_at"],
    )
