"""workflow-svc 路由 —— 工作流管理 API。

权限：超管跨租户；普通用户只能操作本租户（RLS 自动过滤）。
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime

from apihub_core.errors import ApiError, ErrorCode
from apihub_core.tenant import require_tenant
from fastapi import FastAPI, Query
from fastapi.responses import StreamingResponse

from workflow_svc import argo_client
from workflow_svc import repository as repo
from workflow_svc.models import (
    ListWorkflowsQuery,
    LogChunk,
    SubmitWorkflowRequest,
    Workflow,
    WorkflowDetail,
    WorkflowListItem,
    WorkflowStatus,
)


def register_routes(app: FastAPI) -> None:
    # ⚠️ 路由顺序：静态段必须在 {param} 之前
    @app.get("/v1/workflows/health")
    async def health():
        return {"status": "ok", "service": "workflow"}

    @app.post("/v1/workflows", response_model=Workflow, status_code=201)
    async def submit_workflow(req: SubmitWorkflowRequest):
        """提交工作流到 Argo + 写 PG 索引。"""
        ctx = require_tenant()
        tenant_id = int(ctx.tenant_id)

        workflow_uuid = argo_client.gen_workflow_uuid()
        client = argo_client.get_argo_client()

        try:
            argo_name = await client.submit(
                namespace=req.namespace,
                workflow_uuid=workflow_uuid,
                spec=req.spec,
                labels={
                    "tenant_id": str(tenant_id),
                    "api_id": str(req.api_id),
                    "app_id": str(req.app_id),
                    "trace_id": req.trace_id,
                },
            )
        except argo_client.ArgoError as e:
            raise ApiError(
                ErrorCode.INTERNAL,
                f"failed to submit workflow: {e}",
                http_status=502,
            ) from e

        wf_id = await repo.create_workflow(
            tenant_id=tenant_id,
            workflow_uuid=workflow_uuid,
            argo_name=argo_name,
            namespace=req.namespace,
            api_id=req.api_id,
            app_id=req.app_id,
            trace_id=req.trace_id,
            spec=req.spec,
            status=WorkflowStatus.RUNNING,
        )

        # 回读完整行（含 created_at / updated_at）
        detail = await repo.get_workflow(wf_id)
        assert detail is not None
        return detail

    @app.get("/v1/workflows", response_model=list[WorkflowListItem])
    async def list_workflows(
        api_id: int | None = None,
        app_id: int | None = None,
        trace_id: str | None = None,
        status: WorkflowStatus | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ):
        require_tenant()
        query = ListWorkflowsQuery(
            api_id=api_id,
            app_id=app_id,
            trace_id=trace_id,
            status=status,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
        )
        return await repo.list_workflows(query)

    @app.get("/v1/workflows/{workflow_id}", response_model=WorkflowDetail)
    async def get_workflow(workflow_id: int):
        """查询单个工作流详情（含实时 Argo 状态 + steps）。"""
        require_tenant()
        detail = await repo.get_workflow(workflow_id)
        if detail is None:
            raise ApiError(
                ErrorCode.NOT_FOUND,
                f"workflow {workflow_id} not found",
                http_status=404,
            )

        # 同步 Argo 状态
        client = argo_client.get_argo_client()
        try:
            argo_status, msg = await client.get_status(
                namespace=detail.namespace, argo_name=detail.argo_name
            )
            if argo_status != detail.status:
                finished_at = None
                if argo_status in (
                    WorkflowStatus.SUCCEEDED,
                    WorkflowStatus.FAILED,
                    WorkflowStatus.CANCELLED,
                ):
                    finished_at = datetime.now(UTC)
                await repo.update_status(
                    detail.id,
                    status=argo_status,
                    message=msg,
                    finished_at=finished_at,
                )
                detail.status = argo_status
                detail.message = msg
                detail.finished_at = finished_at
            try:
                detail.steps = await client.get_steps(
                    namespace=detail.namespace, argo_name=detail.argo_name
                )
            except argo_client.ArgoError:
                # steps 拉不到不影响主流程
                detail.steps = []
        except argo_client.ArgoError as e:
            raise ApiError(
                ErrorCode.INTERNAL,
                f"failed to query Argo: {e}",
                http_status=502,
            ) from e

        return detail

    @app.post("/v1/workflows/{workflow_id}/cancel")
    async def cancel_workflow(workflow_id: int):
        """取消工作流（Argo spec.shutdown = Stop）。"""
        require_tenant()
        detail = await repo.get_workflow(workflow_id)
        if detail is None:
            raise ApiError(
                ErrorCode.NOT_FOUND,
                f"workflow {workflow_id} not found",
                http_status=404,
            )

        client = argo_client.get_argo_client()
        try:
            await client.cancel(namespace=detail.namespace, argo_name=detail.argo_name)
        except argo_client.ArgoError as e:
            raise ApiError(
                ErrorCode.INTERNAL,
                f"failed to cancel: {e}",
                http_status=502,
            ) from e

        await repo.update_status(
            workflow_id,
            status=WorkflowStatus.CANCELLED,
            finished_at=datetime.now(UTC),
        )
        return {"workflow_id": workflow_id, "status": "cancelled"}

    @app.post("/v1/workflows/{workflow_id}/resume")
    async def resume_workflow(workflow_id: int):
        """恢复工作流（Argo resume 子资源）。"""
        require_tenant()
        detail = await repo.get_workflow(workflow_id)
        if detail is None:
            raise ApiError(
                ErrorCode.NOT_FOUND,
                f"workflow {workflow_id} not found",
                http_status=404,
            )

        client = argo_client.get_argo_client()
        try:
            await client.resume(namespace=detail.namespace, argo_name=detail.argo_name)
        except argo_client.ArgoError as e:
            raise ApiError(
                ErrorCode.INTERNAL,
                f"failed to resume: {e}",
                http_status=502,
            ) from e

        await repo.update_status(workflow_id, status=WorkflowStatus.RUNNING, message="resumed")
        return {"workflow_id": workflow_id, "status": "running"}

    @app.get("/v1/workflows/{workflow_id}/steps")
    async def get_steps(workflow_id: int):
        """查询工作流的所有 step 状态。"""
        require_tenant()
        detail = await repo.get_workflow(workflow_id)
        if detail is None:
            raise ApiError(
                ErrorCode.NOT_FOUND,
                f"workflow {workflow_id} not found",
                http_status=404,
            )

        client = argo_client.get_argo_client()
        try:
            steps = await client.get_steps(namespace=detail.namespace, argo_name=detail.argo_name)
        except argo_client.ArgoError as e:
            raise ApiError(
                ErrorCode.INTERNAL,
                f"failed to get steps: {e}",
                http_status=502,
            ) from e
        return steps

    @app.get("/v1/workflows/{workflow_id}/logs")
    async def stream_logs(
        workflow_id: int,
        step_name: str | None = None,
    ):
        """SSE 流式日志。"""
        require_tenant()
        detail = await repo.get_workflow(workflow_id)
        if detail is None:
            raise ApiError(
                ErrorCode.NOT_FOUND,
                f"workflow {workflow_id} not found",
                http_status=404,
            )

        client = argo_client.get_argo_client()

        async def _gen() -> AsyncIterator[bytes]:
            try:
                async for line in client.stream_logs(
                    namespace=detail.namespace,
                    argo_name=detail.argo_name,
                    step_name=step_name,
                ):
                    chunk = LogChunk(
                        step_name=step_name or "*",
                        line=line.rstrip("\n"),
                        timestamp=datetime.now(UTC),
                    )
                    # SSE 帧：data: {json}\n\n
                    yield f"data: {chunk.model_dump_json()}\n\n".encode()
            except argo_client.ArgoError as e:
                yield f"event: error\ndata: {e}\n\n".encode()

        return StreamingResponse(_gen(), media_type="text/event-stream")
