"""AI 网关 —— /v1/chat/completions 路由。"""

import json
from collections.abc import AsyncIterator
from typing import Any

from apihub_core import kafka
from apihub_core.logging import get_logger
from apihub_core.tenant import require_tenant
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from ai_gateway.crypto import decrypt
from ai_gateway.models import ChatRequest, ChatResponse, ChatResponseChoice, Message, SSEChunk
from ai_gateway.providers import get_provider
from ai_gateway.repository import resolve_model_route

log = get_logger(__name__)

router = APIRouter()


def _to_sse_line(chunk: SSEChunk) -> bytes:
    obj: dict[str, Any] = {
        "id": "chatcmpl-ai-gateway",
        "object": "chat.completion.chunk",
        "choices": [
            {
                "index": 0,
                "delta": {"content": chunk.content} if chunk.content else {},
                "finish_reason": chunk.finish_reason,
            }
        ],
    }
    if chunk.usage:
        obj["usage"] = chunk.usage
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode()


async def _emit_ai_call(
    tenant_id: str,
    app_id: str | None,
    model: str,
    stream: bool,
    usage: dict | None,
) -> None:
    """计量事件 → api-call-events（计费/审计/看板都吃这条流，缺失即免费白嫖）。"""
    try:
        await kafka.emit(
            "api-call-events",
            {
                "tenant_id": tenant_id,
                "app_id": app_id,
                "api_id": f"ai:{model}",
                "event_type": "ai_call",
                "allowed": True,
                "stream": stream,
                "model": model,
                "total_tokens": (usage or {}).get("total_tokens"),
            },
            key=f"{tenant_id}:{app_id or '-'}:ai:{model}",
        )
    except Exception as e:  # 计量失败不阻断响应，但必须留痕
        log.warning("ai_call_emit_failed", model=model, error=str(e))


@router.post("/v1/chat/completions")
async def chat_completions(payload: ChatRequest):
    # 鉴权：租户上下文由中间件注入（main.py 不再跳过本路径）
    ctx = require_tenant()

    route = await resolve_model_route(payload.model)
    if not route:
        raise HTTPException(status_code=400, detail=f"model '{payload.model}' not supported")

    api_key = decrypt(route.provider_key_encrypted)
    provider = get_provider(route.provider_type)

    stream = payload.stream if payload.stream is not None else True
    provider_iter = provider.chat_completion(
        messages=[m.model_dump() for m in payload.messages],
        model=route.target_model,
        api_key=api_key,
        base_url=route.base_url,
        stream=stream,
        temperature=payload.temperature,
        max_tokens=payload.max_tokens,
        extra_body=payload.extra_body,
    )

    if not stream:
        async for chunk in provider_iter:
            await _emit_ai_call(ctx.tenant_id, ctx.app_id, payload.model, False, chunk.usage)
            return ChatResponse(
                choices=[
                    ChatResponseChoice(
                        index=0,
                        message=Message(role="assistant", content=chunk.content),
                        finish_reason="stop",
                    )
                ],
                usage=chunk.usage or {},
            )
        await _emit_ai_call(ctx.tenant_id, ctx.app_id, payload.model, False, None)
        return ChatResponse(choices=[], usage={})

    # 流式响应体在中间件 finally（清租户上下文）之后才执行，
    # 先取快照再进生成器。
    tenant_id, app_id = ctx.tenant_id, ctx.app_id
    return StreamingResponse(
        _stream_sse(provider_iter, tenant_id, app_id, payload.model),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no"},
    )


async def _stream_sse(
    provider_iter: AsyncIterator[SSEChunk],
    tenant_id: str,
    app_id: str | None,
    model: str,
) -> AsyncIterator[bytes]:
    last_usage: dict | None = None
    async for chunk in provider_iter:
        if chunk.usage:
            last_usage = chunk.usage
        yield _to_sse_line(chunk)
        if chunk.finish_reason:
            break
    await _emit_ai_call(tenant_id, app_id, model, True, last_usage)
    yield b"data: [DONE]\n\n"
