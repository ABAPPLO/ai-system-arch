"""异步任务派发 —— 接收请求 → 写 PG task 记录 → 推 Kafka task-requests → 返回 task_id。

详见 docs/05-core-flows.md §4 异步任务时序。
"""

import uuid

from apihub_core import db, kafka
from apihub_core.events import TaskRequest
from apihub_core.tenant import require_tenant
from fastapi import Request
from fastapi.responses import JSONResponse

from dispatcher.event import new_request_id
from dispatcher.models import ApiVersionSnapshot


async def dispatch_async_task(snap: ApiVersionSnapshot, request: Request) -> JSONResponse:
    """异步任务专用入口。

    流程：
    1. 在 PG task 表插一行（pending）
    2. 推 Kafka task-requests（executor 消费后跑实际工作）
    3. 立即返回 task_id（HTTP 202 Accepted）
    """
    ctx = require_tenant()
    task_id = f"task_{uuid.uuid4().hex[:16]}"
    request_id = request.headers.get("X-Request-Id") or new_request_id()
    body = await request.body()

    # 写 task 表（保证 at-least-once：DB 提交后再发 Kafka）
    async with db.db_session() as conn:
        await conn.execute(
            """
            INSERT INTO task (
                id, tenant_id, api_id, api_version_id, app_id, status,
                payload, request_id, created_at
            ) VALUES ($1, $2, $3, $4, $5, 'pending', $6, $7, NOW())
            """,
            task_id,
            ctx.tenant_id,
            snap.api_id,
            snap.id,
            ctx.app_id,
            body.decode("utf-8", errors="replace"),
            request_id,
        )

    # 投递任务请求（executor 消费）
    await kafka.emit_event(
        TaskRequest(
            task_id=task_id,
            api_id=snap.api_id,
            api_version_id=snap.id,
            backend_url=snap.backend_url,
            payload=body.decode("utf-8", errors="replace"),
            request_id=request_id,
        )
    )

    return JSONResponse(
        status_code=202,
        content={
            "task_id": task_id,
            "status": "pending",
            "trace_id": request.headers.get("X-Trace-Id", ""),
        },
    )
