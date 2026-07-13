import json
from collections.abc import AsyncIterator

import httpx

from ai_gateway.models import SSEChunk
from ai_gateway.providers import BaseProvider


class OpenAICompatibleProvider(BaseProvider):
    async def chat_completion(
        self, messages, model, api_key, base_url, stream=True,
        temperature=None, max_tokens=None, extra_body=None,
    ) -> AsyncIterator[SSEChunk]:
        url = f"{base_url.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {"model": model, "messages": messages, "stream": stream}
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if extra_body:
            payload.update(extra_body)

        async with httpx.AsyncClient(timeout=120.0) as client:
            if not stream:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                usage = data.get("usage", {})
                content = data["choices"][0]["message"]["content"]
                yield SSEChunk(content=content, finish_reason="stop", usage=usage)
                return

            async with client.stream("POST", url, json=payload, headers=headers) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:].strip()
                    if data_str == "[DONE]":
                        yield SSEChunk(finish_reason="stop")
                        return
                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    choice = data.get("choices", [{}])[0]
                    delta = choice.get("delta", {})
                    yield SSEChunk(
                        content=delta.get("content", ""),
                        finish_reason=choice.get("finish_reason"),
                        usage=data.get("usage"),
                    )
