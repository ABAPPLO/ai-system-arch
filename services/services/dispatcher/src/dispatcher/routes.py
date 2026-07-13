"""dispatcher 路由 —— 单一 catch-all 入口。

入口：ANY /dispatch/{rest:path}
解析：优先 X-API-Version-Id header，回退 path 匹配
"""

import uuid

from apihub_core.config import get_settings
from apihub_core.errors import ApiError, ErrorCode
from apihub_core.logging import get_logger
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from opentelemetry import trace

from dispatcher.forwarder import HttpForwarder
from dispatcher.models import SubmitJobRequest
from dispatcher.resolver import resolve_by_header, resolve_by_path
from dispatcher.task_dispatcher import dispatch_async_task

log = get_logger(__name__)

# 模块级 forwarder（init_app 时注入 client）
_forwarder: HttpForwarder | None = None


def set_forwarder(f: HttpForwarder) -> None:
    global _forwarder
    _forwarder = f


def get_forwarder() -> HttpForwarder:
    if _forwarder is None:
        raise RuntimeError("Forwarder not initialized")
    return _forwarder


def _trace_id() -> str:
    """从 OTel 当前 span 取 trace_id，缺则生成一个 32hex。"""
    span = trace.get_current_span()
    ctx = span.get_span_context() if span is not None else None
    if ctx is not None and ctx.is_valid:
        return f"{ctx.trace_id:032x}"
    return uuid.uuid4().hex


def _wf_client(request: Request):
    """从 app.state 取 workflow 代理 client；未初始化则 500。"""
    client = getattr(request.app.state, "workflow_client", None)
    if client is None:
        raise ApiError(
            ErrorCode.INTERNAL,
            "workflow client not initialized",
            http_status=500,
        )
    return client


def register_routes(app: FastAPI) -> None:
    @app.api_route(
        "/dispatch/{rest:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    )
    async def dispatch(request: Request):
        rest = request.path_params["rest"]
        method = request.method

        # 解析接口元数据
        version_id = request.headers.get("X-API-Version-Id")
        if version_id:
            snap = await resolve_by_header(version_id)
        else:
            full_path = f"/{rest}"
            snap = await resolve_by_path(method, full_path)

        # 应用层 visibility 授权（public / tenant / private 三级）。
        # resolve 用 meta_db_session 跨租户拿到 snap，这里按 caller TenantContext
        # 做授权：public 放行；tenant 同租户；private 同租户 + 平台超管。否则 403。
        from apihub_core.tenant import get_tenant_context

        from dispatcher.visibility import check_visibility

        ctx = get_tenant_context()
        if ctx is not None:
            check_visibility(snap, ctx)

        # 按 backend_type 分流
        if snap.backend_type == "async_task":
            return await dispatch_async_task(snap, request)

        if snap.backend_type == "workflow":
            # workflow 走独立入口 POST /v1/jobs（见下方 submit_job），/dispatch 不受理
            raise ApiError(
                ErrorCode.INTERNAL,
                "workflow backend: use POST /v1/jobs (not /dispatch)",
                http_status=501,
            )

        # http / ai_model
        # 沙箱模式：X-Environment: sandbox → 路由到 mock-backend
        if request.headers.get("X-Environment", "").lower() == "sandbox":
            from dataclasses import replace
            snap = replace(
                snap,
                backend_url=f"http://mock-backend.apihub-system/dispatch{rest}",
            )
        return await get_forwarder().forward(snap, request)

    @app.get("/v1/dispatcher/health")
    async def health():
        return {"status": "ok", "service": "dispatcher"}

    # ---- workflow 入口（文档 §4）：代理到 workflow-svc ----
    @app.post("/v1/jobs", status_code=201)
    async def submit_job(body: SubmitJobRequest, request: Request):
        """workflow 入口（文档 §4）：代理到 workflow-svc POST /v1/workflows。

        body 经 Pydantic 校验：缺 api_id/app_id/spec 或非 JSON → 422（不再 500）。
        """
        settings = get_settings()
        wf_body = {
            "api_id": body.api_id,
            "app_id": body.app_id,
            "spec": body.spec,
            "trace_id": body.trace_id or _trace_id(),
            "namespace": body.namespace,
        }
        client = _wf_client(request)
        resp = await client.post(
            f"{settings.workflow_service_url}/v1/workflows",
            json=wf_body,
            headers={"X-API-Key": request.headers.get("X-API-Key", "")},
        )
        if resp.status_code >= 400:
            raise ApiError(
                ErrorCode.INTERNAL,
                f"workflow-svc error: {resp.text[:300]}",
                http_status=502,
            )
        return JSONResponse(status_code=201, content=resp.json())

    @app.get("/v1/jobs/{job_id}")
    async def get_job(job_id: int, request: Request):
        """workflow 轮询：代理到 workflow-svc GET /v1/workflows/{id}。"""
        settings = get_settings()
        client = _wf_client(request)
        resp = await client.get(
            f"{settings.workflow_service_url}/v1/workflows/{job_id}",
            headers={"X-API-Key": request.headers.get("X-API-Key", "")},
        )
        if resp.status_code == 404:
            raise ApiError(
                ErrorCode.NOT_FOUND,
                f"job {job_id} not found",
                http_status=404,
            )
        if resp.status_code >= 400:
            raise ApiError(
                ErrorCode.INTERNAL,
                f"workflow-svc error: {resp.text[:300]}",
                http_status=502,
            )
        return resp.json()

    @app.post("/v1/jobs/{job_id}/cancel")
    async def cancel_job(job_id: int, request: Request):
        """workflow cancel 代理：→ workflow-svc POST /v1/workflows/{id}/cancel。"""
        settings = get_settings()
        client = _wf_client(request)
        resp = await client.post(
            f"{settings.workflow_service_url}/v1/workflows/{job_id}/cancel",
            headers={"X-API-Key": request.headers.get("X-API-Key", "")},
        )
        if resp.status_code == 404:
            raise ApiError(
                ErrorCode.NOT_FOUND,
                f"job {job_id} not found",
                http_status=404,
            )
        if resp.status_code >= 400:
            raise ApiError(
                ErrorCode.INTERNAL,
                f"workflow-svc error: {resp.text[:300]}",
                http_status=502,
            )
        return JSONResponse(status_code=200, content=resp.json())

    @app.post("/v1/jobs/{job_id}/resume")
    async def resume_job(job_id: int, request: Request):
        """workflow resume 代理：→ workflow-svc POST /v1/workflows/{id}/resume。"""
        settings = get_settings()
        client = _wf_client(request)
        resp = await client.post(
            f"{settings.workflow_service_url}/v1/workflows/{job_id}/resume",
            headers={"X-API-Key": request.headers.get("X-API-Key", "")},
        )
        if resp.status_code == 404:
            raise ApiError(
                ErrorCode.NOT_FOUND,
                f"job {job_id} not found",
                http_status=404,
            )
        if resp.status_code >= 400:
            raise ApiError(
                ErrorCode.INTERNAL,
                f"workflow-svc error: {resp.text[:300]}",
                http_status=502,
            )
        return JSONResponse(status_code=200, content=resp.json())

    @app.get("/v1/jobs/{job_id}/logs")
    async def stream_job_logs(job_id: int, request: Request, step_name: str | None = None):
        """workflow logs(SSE) 代理：→ workflow-svc GET /v1/workflows/{id}/logs。

        Arg 已完成 wf 的日志 Argo 一次性返回，故用缓冲式透传（非长连真流式）。
        真 SSE 长流式见 spec §10（后续）。
        """
        settings = get_settings()
        client = _wf_client(request)
        params = {"step_name": step_name} if step_name else None
        resp = await client.get(
            f"{settings.workflow_service_url}/v1/workflows/{job_id}/logs",
            headers={"X-API-Key": request.headers.get("X-API-Key", "")},
            params=params,
        )
        if resp.status_code == 404:
            raise ApiError(
                ErrorCode.NOT_FOUND,
                f"job {job_id} not found",
                http_status=404,
            )
        if resp.status_code >= 400:
            raise ApiError(
                ErrorCode.INTERNAL,
                f"workflow-svc error: {resp.text[:300]}",
                http_status=502,
            )
        return Response(content=resp.content, media_type="text/event-stream")
