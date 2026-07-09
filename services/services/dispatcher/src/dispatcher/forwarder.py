"""HTTP 转发 —— 同步 + AI 流式。

设计要点：
1. 用 httpx.AsyncClient 单例（启动时创建，关闭时释放），复用 TCP 连接
2. 透传 trace context（OTel 自动注入到 httpx header）
3. 不透传的 header：host / content-length（httpx 自管）/ authorization（业务 backend 用自己的）
4. AI 流式：直接 StreamingResponse，chunk-by-chunk 转发 SSE
"""

import time
from collections.abc import AsyncIterator

import httpx
from apihub_core.errors import ApiError, ErrorCode
from apihub_core.logging import get_logger
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from dispatcher.event import build_call_event, new_request_id
from dispatcher.masking import apply_masking

log = get_logger(__name__)

# 不透传给后端的 header
_DROP_HEADERS = {
    "host", "content-length", "connection", "transfer-encoding",
    "x-api-key", "authorization",  # 调用方凭证不能给后端
    "x-tenant-id", "x-app-id",     # 由 dispatcher 自己注入
}

# AI SSE chunk 用于解析 token usage 的最小解析（容错）
import json as _json  # noqa: E402


class HttpForwarder:
    """HTTP 同步转发器。"""

    def __init__(self, client: httpx.AsyncClient):
        self._client = client

    async def forward(self, snap, request: Request) -> JSONResponse | StreamingResponse:
        """根据 snap.backend_type 转发。

        - http       → 同步转发
        - ai_model + ai_streaming=True → SSE 流式
        - ai_model + ai_streaming=False → 同步转发（OpenAI 非流式）
        - async_task / workflow → 不走这里，由 task_dispatcher 处理
        """
        body = await request.body()
        forward_headers = _build_forward_headers(request)
        url = _render_url(snap.backend_url, request)

        request_id = request.headers.get("X-Request-Id") or new_request_id()
        start = time.perf_counter()

        if snap.is_streaming:
            return await self._forward_stream(snap, request, url, forward_headers, body, request_id, start)

        return await self._forward_sync(snap, request, url, forward_headers, body, request_id, start)

    async def _forward_sync(self, snap, request, url, headers, body, request_id, start):
        try:
            resp = await self._client.request(
                method=request.method,
                url=url,
                headers=headers,
                params=request.query_params,
                content=body,
                timeout=snap.timeout_ms / 1000,
            )
        except httpx.RequestError as e:
            backend_latency_ms = int((time.perf_counter() - start) * 1000)
            await _emit_failure(snap, request, e, request_id, backend_latency_ms)
            raise ApiError(  # noqa: B904
                ErrorCode.API_DOWN,
                f"backend unreachable: {e}",
                http_status=503,
            )

        backend_latency_ms = int((time.perf_counter() - start) * 1000)
        response_body = resp.content

        # 脱敏响应（仅日志用，返回调用方原 body）
        masked_for_log = apply_masking(_safe_json(response_body), _rules_from_snap(snap))

        await _emit_success(
            snap, request, resp.status_code, len(body), len(response_body),
            backend_latency_ms, request_id, ai_usage=_extract_ai_usage(snap, response_body),
            masked_payload=masked_for_log,
        )

        # 透传响应 header（白名单）
        resp_headers = _filter_response_headers(resp.headers)

        return JSONResponse(
            status_code=resp.status_code,
            content=_safe_json(response_body),
            headers=resp_headers,
        )

    async def _forward_stream(self, snap, request, url, headers, body, request_id, start):
        """AI SSE 流式 —— chunk-by-chunk 转发 + 末尾 emit 含 token 用量。"""

        async def stream_and_emit() -> AsyncIterator[bytes]:
            tokens_prompt = 0
            tokens_completion = 0
            total_bytes = 0
            status_code = 200

            try:
                async with self._client.stream(
                    method=request.method,
                    url=url,
                    headers=headers,
                    params=request.query_params,
                    content=body,
                    timeout=None,  # 流式不超时（依赖 backend 自己控）
                ) as resp:
                    status_code = resp.status_code
                    async for chunk in resp.aiter_bytes():
                        total_bytes += len(chunk)
                        # 解析 SSE 试取 token usage
                        p, c = _extract_tokens_from_chunk(chunk)
                        tokens_prompt += p
                        tokens_completion += c
                        yield chunk
            except httpx.RequestError as e:
                log.warning("stream_backend_error", error=str(e))
                # 给客户端一个 SSE 错误事件
                yield b"data: {\"error\":\"backend error\"}\n\n"
                status_code = 503
            finally:
                latency_ms = int((time.perf_counter() - start) * 1000)
                await _emit_stream_complete(
                    snap, request, status_code, len(body), total_bytes,
                    latency_ms, request_id,
                    tokens_prompt=tokens_prompt,
                    tokens_completion=tokens_completion,
                )

        return StreamingResponse(
            stream_and_emit(),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},  # nginx / APISIX 关缓冲
        )


def _build_forward_headers(request: Request) -> dict[str, str]:
    out = {}
    for k, v in request.headers.items():
        if k.lower() in _DROP_HEADERS:
            continue
        out[k] = v
    return out


def _filter_response_headers(headers) -> dict[str, str]:
    allow = {"content-type", "x-request-id", "cache-control", "etag"}
    return {k: v for k, v in headers.items() if k.lower() in allow}


def _render_url(backend_url: str, request: Request) -> str:
    """把 backend_url 中的 {path_var} 用 request 路径变量替换。

    简化版：把请求 path 多余段直接拼到 backend_url。
    完整实现应在 resolver 中匹配后回填 path_params 到 snap。
    """
    if "{" not in backend_url:
        return backend_url
    # 占位：实际匹配在 resolver 完成（dev 阶段简化）
    return backend_url


def _safe_json(body: bytes):
    if not body:
        return None
    try:
        import json
        return json.loads(body)
    except Exception:
        # 非 JSON，原样返回让上层透传
        return body.decode("utf-8", errors="replace")


def _rules_from_snap(snap) -> list[dict] | None:
    if not snap.masking:
        return None
    return snap.masking.get("response")


def _extract_ai_usage(snap, body: bytes) -> dict:
    """从非流式 AI 响应中提取 token 用量。"""
    if snap.backend_type != "ai_model":
        return {}
    data = _safe_json(body)
    if isinstance(data, dict) and "usage" in data:
        u = data["usage"]
        return {
            "token_prompt": u.get("prompt_tokens", 0),
            "token_completion": u.get("completion_tokens", 0),
            "token_total": u.get("total_tokens", 0),
        }
    return {}


def _extract_tokens_from_chunk(chunk: bytes) -> tuple[int, int]:
    """从 SSE chunk 中尽量解析 token usage。

    OpenAI 流式：最后一个 chunk（带 finish_reason）的 usage 字段才完整。
    """
    prompt = 0
    completion = 0
    try:
        text = chunk.decode("utf-8", errors="ignore")
        for line in text.splitlines():
            if not line.startswith("data: "):
                continue
            data_str = line[6:].strip()
            if data_str == "[DONE]":
                continue
            data = _json.loads(data_str)
            if "usage" in data:
                u = data["usage"]
                prompt = u.get("prompt_tokens", 0)
                completion = u.get("completion_tokens", 0)
    except Exception:  # noqa: S110
        pass
    return prompt, completion


async def _emit_success(snap, request, status_code, req_size, resp_size,
                       backend_latency_ms, request_id, ai_usage=None, masked_payload=None):
    from apihub_core import kafka
    payload = build_call_event(
        api_id=snap.api_id,
        api_version_id=snap.id,
        method=request.method,
        path=str(request.url.path),
        status_code=status_code,
        is_success=200 <= status_code < 400,
        latency_ms=backend_latency_ms,
        request_size=req_size,
        response_size=resp_size,
        backend_type=snap.backend_type,
        backend_latency_ms=backend_latency_ms,
        user_agent=request.headers.get("user-agent", ""),
        client_ip=request.client.host if request.client else "0.0.0.0",
        ai_model=snap.ai_model or "",
        ai_streaming=snap.ai_streaming,
        token_prompt=ai_usage.get("token_prompt", 0) if ai_usage else 0,
        token_completion=ai_usage.get("token_completion", 0) if ai_usage else 0,
        token_total=ai_usage.get("token_total", 0) if ai_usage else 0,
        request_id=request_id,
        trace_id=request.headers.get("X-Trace-Id", ""),
    )
    await kafka.emit("api-call-events", payload)


async def _emit_failure(snap, request, exc, request_id, backend_latency_ms):
    from apihub_core import kafka
    payload = build_call_event(
        api_id=snap.api_id,
        api_version_id=snap.id,
        method=request.method,
        path=str(request.url.path),
        status_code=503,
        is_success=False,
        latency_ms=backend_latency_ms,
        request_size=0,
        response_size=0,
        backend_type=snap.backend_type,
        backend_latency_ms=backend_latency_ms,
        error_code=ErrorCode.API_DOWN.name,
        error_msg=str(exc)[:500],
        user_agent=request.headers.get("user-agent", ""),
        client_ip=request.client.host if request.client else "0.0.0.0",
        request_id=request_id,
        trace_id=request.headers.get("X-Trace-Id", ""),
    )
    await kafka.emit("api-call-events", payload)


async def _emit_stream_complete(snap, request, status_code, req_size, total_bytes,
                                 latency_ms, request_id, tokens_prompt=0, tokens_completion=0):
    from apihub_core import kafka
    payload = build_call_event(
        api_id=snap.api_id,
        api_version_id=snap.id,
        method=request.method,
        path=str(request.url.path),
        status_code=status_code,
        is_success=200 <= status_code < 400,
        latency_ms=latency_ms,
        request_size=req_size,
        response_size=total_bytes,
        backend_type=snap.backend_type,
        backend_latency_ms=latency_ms,
        ai_model=snap.ai_model or "",
        ai_streaming=True,
        token_prompt=tokens_prompt,
        token_completion=tokens_completion,
        token_total=tokens_prompt + tokens_completion,
        user_agent=request.headers.get("user-agent", ""),
        client_ip=request.client.host if request.client else "0.0.0.0",
        request_id=request_id,
        trace_id=request.headers.get("X-Trace-Id", ""),
    )
    await kafka.emit("api-call-events", payload)


def _b(s: str) -> bytes:
    return s.encode()
