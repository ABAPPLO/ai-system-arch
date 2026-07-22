import json
from collections.abc import AsyncIterator

import httpx

from ai_gateway.models import SSEChunk
from ai_gateway.providers import BaseProvider


def _openai_to_anthropic_messages(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        role = "assistant" if m.get("role") == "assistant" else "user"
        content = m.get("content", "")
        if isinstance(content, str):
            out.append({"role": role, "content": [{"type": "text", "text": content}]})
        else:
            out.append({"role": role, "content": content})
    return out


def _extract_text(content_blocks: list) -> str:
    texts = [b.get("text", "") for b in content_blocks if b.get("type") == "text"]
    return "\n".join(texts)


def _map_stop_reason(reason: str) -> str:
    return {"end_turn": "stop", "max_tokens": "length", "stop_sequence": "stop"}.get(reason, reason)


class AnthropicProvider(BaseProvider):
    async def chat_completion(
        self,
        messages,
        model,
        api_key,
        base_url,
        stream=True,
        temperature=None,
        max_tokens=None,
        extra_body=None,
    ) -> AsyncIterator[SSEChunk]:
        url = f"{base_url.rstrip('/')}/v1/messages"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

        # Extract system message — Anthropic requires it as a top-level field
        system_content = None
        for m in messages:
            if m.get("role") == "system":
                system_content = m.get("content", "")
                break
        non_system = [m for m in messages if m.get("role") != "system"]

        payload = {
            "model": model,
            "messages": _openai_to_anthropic_messages(non_system),
            "max_tokens": max_tokens or 1024,
            "stream": stream,
        }
        if system_content:
            payload["system"] = system_content
        if temperature is not None:
            payload["temperature"] = temperature
        if extra_body:
            payload.update(extra_body)

        async with httpx.AsyncClient(timeout=120.0) as client:
            if not stream:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                content = _extract_text(data.get("content", []))
                ud = data.get("usage", {})
                usage = {
                    "prompt_tokens": ud.get("input_tokens", 0),
                    "completion_tokens": ud.get("output_tokens", 0),
                    "total_tokens": ud.get("input_tokens", 0) + ud.get("output_tokens", 0),
                }
                yield SSEChunk(content=content, finish_reason="stop", usage=usage)
                return

            async with client.stream("POST", url, json=payload, headers=headers) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if line.startswith("event: "):
                        continue
                    if not line.startswith("data: "):
                        continue
                    try:
                        data = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue
                    et = data.get("type", "")
                    if et == "content_block_delta":
                        delta = data.get("delta", {})
                        if delta.get("type") == "text_delta":
                            yield SSEChunk(content=delta.get("text", ""))
                    elif et == "message_delta":
                        ud = data.get("usage") or {}
                        usage = {
                            "prompt_tokens": ud.get("input_tokens", 0),
                            "completion_tokens": ud.get("output_tokens", 0),
                            "total_tokens": ud.get("input_tokens", 0) + ud.get("output_tokens", 0),
                        }
                        yield SSEChunk(
                            finish_reason=_map_stop_reason(data.get("stop_reason")), usage=usage
                        )
                    elif et == "message_stop":
                        yield SSEChunk(finish_reason="stop")
