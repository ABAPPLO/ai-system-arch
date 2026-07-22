"""AI 网关 —— /v1/chat/completions 路由。"""

import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from ai_gateway.crypto import decrypt
from ai_gateway.models import ChatRequest, ChatResponse, ChatResponseChoice, Message, SSEChunk
from ai_gateway.providers import get_provider
from ai_gateway.repository import resolve_model_route

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


@router.post("/v1/chat/completions")
async def chat_completions(payload: ChatRequest):
    route = await resolve_model_route(payload.model)
    if not route:
        raise HTTPException(status_code=400, detail=f"model '{payload.model}' not supported")

    api_key = decrypt(route.provider_key_encrypted)
    provider = get_provider(route.provider_type)

    provider_iter = provider.chat_completion(
        messages=[m.model_dump() for m in payload.messages],
        model=route.target_model,
        api_key=api_key,
        base_url=route.base_url,
        stream=payload.stream if payload.stream is not None else True,
        temperature=payload.temperature,
        max_tokens=payload.max_tokens,
        extra_body=payload.extra_body,
    )

    if payload.stream is False:
        async for chunk in provider_iter:
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
        return ChatResponse(choices=[], usage={})

    return StreamingResponse(
        _stream_sse(provider_iter),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no"},
    )


async def _stream_sse(
    provider_iter: AsyncIterator[SSEChunk],
) -> AsyncIterator[bytes]:
    async for chunk in provider_iter:
        yield _to_sse_line(chunk)
        if chunk.finish_reason:
            break
    yield b"data: [DONE]\n\n"
