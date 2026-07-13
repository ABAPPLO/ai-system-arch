# AI 网关 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建独立 ai-gateway 服务，实现 LLM 推理路由层——多 Provider 插件化接入（OpenAI 兼容 + Anthropic）、模型路由、Provider API Key 加密存储、OpenAI 兼容 `/v1/chat/completions` 端点。

**Architecture:** 新服务 `ai-gateway`（:8013）作为 dispatcher 的 `backend_url` 目标，dispatcher _forward_stream 现有 SSE 透传逻辑不改。ai-gateway 通过 PG 路由表 + Provider 插件适配上游 LLM API，输出归一化的 OpenAI SSE 格式（含 usage），dispatcher 现有 `_extract_tokens_from_chunk` 自动提取 token 并上报。

**Tech Stack:** Python 3.11+ FastAPI + asyncpg + httpx / AES-256-GCM

## Global Constraints

- `admin_db_session()` 用于所有 PG 配置查询（`ai_provider` / `ai_model_route` 是平台基础设施表，无 RLS）
- SSE 输出必须是 OpenAI 兼容格式（`data: {...}\n\n`），最终 chunk 携带 `usage`（`prompt_tokens`, `completion_tokens`, `total_tokens`）
- Provider API Key 只解密到内存，不落日志、不返回给调用方
- Key 加密使用 `AES-256-GCM`，密钥来自环境变量 `AI_GATEWAY_ENCRYPTION_KEY`（32 字节 hex）
- `ruff check` + `mypy` clean before commit
- 端口 8013（notification 已占 8012）
- 遵循现有服务模式：`create_app()` from apihub-core，`admin_db_session()` 读 PG
- 不依赖 Kafka（token 上报由 dispatcher 现有逻辑完成）

---

### Task 1: crypto.py — AES-256-GCM 加解密

**Files:**
- Create: `services/services/ai-gateway/src/ai_gateway/crypto.py`

**Interfaces:**
- Consumes: `AI_GATEWAY_ENCRYPTION_KEY` from settings (32 bytes hex)
- Produces: `encrypt(plaintext: str) -> str` / `decrypt(ciphertext: str) -> str` — consumed by repository.py

- [ ] **Step 1: Write `crypto.py`**

```python
"""AES-256-GCM 加解密 —— Provider API Key 加密存储。

密钥来源：环境变量 AI_GATEWAY_ENCRYPTION_KEY（32 字节 hex 字符串）。
密文格式：base64(nonce + ciphertext + tag)。
"""

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from apihub_core.config import get_settings

_NONCE_LENGTH = 12  # AES-GCM 推荐 96-bit nonce


def _get_key() -> bytes:
    key_hex = get_settings().ai_gateway_encryption_key
    if not key_hex:
        raise RuntimeError("AI_GATEWAY_ENCRYPTION_KEY not configured")
    return bytes.fromhex(key_hex)


def encrypt(plaintext: str) -> str:
    """加密明文 → base64(nonce + ciphertext + tag)。"""
    key = _get_key()
    aesgcm = AESGCM(key)
    nonce = os.urandom(_NONCE_LENGTH)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(nonce + ciphertext).decode("ascii")


def decrypt(ciphertext_b64: str) -> str:
    """解密 base64(nonce + ciphertext + tag) → 明文。"""
    key = _get_key()
    raw = base64.b64decode(ciphertext_b64)
    nonce = raw[:_NONCE_LENGTH]
    ct = raw[_NONCE_LENGTH:]
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ct, None).decode("utf-8")
```

- [ ] **Step 2: Create `pyproject.toml`**

```toml
[project]
name = "ai-gateway"
version = "0.1.0"
description = "AI 网关 —— LLM 推理路由、多 Provider 接入、Token 计费"
requires-python = ">=3.11"
dependencies = [
  "apihub-core",
  "cryptography>=42.0",
]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]
```

- [ ] **Step 3: Install + verify**

```bash
pip install -e services/services/ai-gateway
python -c "from ai_gateway.crypto import encrypt, decrypt; print('OK')"
```
Expected: `OK`

- [ ] **Step 4: Smoke test roundtrip**

```bash
python -c "
import os; os.environ['AI_GATEWAY_ENCRYPTION_KEY'] = 'a'*64
from apihub_core.config import get_settings; get_settings.cache_clear()
from ai_gateway.crypto import encrypt, decrypt
ct = encrypt('sk-test-key-12345')
pt = decrypt(ct)
assert pt == 'sk-test-key-12345', f'roundtrip failed: {pt}'
print('✅ roundtrip OK')
"
```
Expected: `✅ roundtrip OK`

- [ ] **Step 5: Commit**

```bash
git add services/services/ai-gateway/
git commit -m "feat(ai-gateway): AES-256-GCM 加解密 + pyproject.toml"
```

---

### Task 2: models.py — Pydantic 模型

**Files:**
- Create: `services/services/ai-gateway/src/ai_gateway/models.py`

**Interfaces:**
- Consumes: nothing
- Produces: `ChatRequest`, `SSEChunk`, `RouteResult` — consumed by routes.py + repository.py + providers

- [ ] **Step 1: Write `models.py`**

```python
"""AI 网关 Pydantic 模型。"""

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel


class Message(BaseModel):
    role: str = "user"
    content: str = ""


class ChatRequest(BaseModel):
    model: str
    messages: list[Message] = []
    stream: bool | None = True
    temperature: float | None = None
    max_tokens: int | None = None
    extra_body: dict[str, Any] | None = None


class ChatResponseChoice(BaseModel):
    index: int = 0
    message: Message | None = None
    finish_reason: str | None = None


class ChatResponse(BaseModel):
    id: str = ""
    object: str = "chat.completion"
    choices: list[ChatResponseChoice] = []
    usage: dict[str, int] = {}


@dataclass
class SSEChunk:
    content: str = ""
    finish_reason: str | None = None
    usage: dict | None = None


@dataclass
class RouteResult:
    target_provider_id: str
    target_model: str
    provider_type: str
    base_url: str
    provider_key_encrypted: str
```

- [ ] **Step 2: Verify import**

```bash
python -c "from ai_gateway.models import ChatRequest, SSEChunk, RouteResult; print('OK')"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add services/services/ai-gateway/src/ai_gateway/models.py
git commit -m "feat(ai-gateway): Pydantic 模型定义"
```

---

### Task 3: DB migration + apihub-core config

**Files:**
- Create: `scripts/init-db/07-ai-gateway.sql`
- Modify: `services/libs/apihub-core/src/apihub_core/config.py`

- [ ] **Step 1: Write `07-ai-gateway.sql`**

```sql
-- Phase 4 AI 网关 —— ai_provider / ai_provider_key / ai_model_route

CREATE TABLE IF NOT EXISTS ai_provider (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL UNIQUE,
    provider_type   TEXT NOT NULL CHECK (provider_type IN ('openai_compatible', 'anthropic')),
    base_url        TEXT NOT NULL,
    default_model   TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ai_provider_key (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    provider_id     UUID NOT NULL REFERENCES ai_provider(id) ON DELETE CASCADE,
    key_alias       TEXT NOT NULL DEFAULT '',
    key_encrypted   TEXT NOT NULL,
    key_prefix      TEXT NOT NULL DEFAULT '',
    expires_at      TIMESTAMPTZ,
    status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'expired', 'revoked')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ai_model_route (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    model_pattern       TEXT NOT NULL,
    target_provider_id  UUID NOT NULL REFERENCES ai_provider(id),
    target_model        TEXT NOT NULL,
    priority            INT NOT NULL DEFAULT 0,
    status              TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

- [ ] **Step 2: Add config field**

Insert after `jwt_refresh_ttl_seconds` in `config.py`:

```python
    ai_gateway_encryption_key: str = ""   # AES-256 密钥（32 字节 hex），必填
```

- [ ] **Step 3: Run migration**

```bash
make dev-up
python -c "
import asyncio, asyncpg
async def run():
    conn = await asyncpg.connect(user='apihub', password='apihub_dev_pwd', database='apihub', host='127.0.0.1', port=5432)
    with open('scripts/init-db/07-ai-gateway.sql') as f:
        await conn.execute(f.read())
    rows = await conn.fetch(\"SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_name LIKE 'ai_%'\")
    print('tables:', [r['table_name'] for r in rows])
    await conn.close()
asyncio.run(run())
"
```
Expected: `tables: ['ai_provider', 'ai_provider_key', 'ai_model_route']`

- [ ] **Step 4: Commit**

```bash
git add scripts/init-db/07-ai-gateway.sql services/libs/apihub-core/src/apihub_core/config.py
git commit -m "feat(ai-gateway): PG 表 + config"
```

---

### Task 4: repository.py — 路由查询 + key 解密

**Files:**
- Create: `services/services/ai-gateway/src/ai_gateway/repository.py`

**Interfaces:**
- Consumes: `RouteResult` from models.py, `decrypt()` from crypto.py, `admin_db_session()` from apihub-core
- Produces: `resolve_model_route(model: str) -> RouteResult | None`

- [ ] **Step 1: Write `repository.py`**

```python
"""AI 网关数据访问 —— 模型路由查询 + Provider Key 解密。"""

from apihub_core import db

from ai_gateway.crypto import decrypt
from ai_gateway.models import RouteResult


async def resolve_model_route(model: str) -> RouteResult | None:
    async with db.admin_db_session() as conn:
        row = await conn.fetchrow(
            """
            SELECT mr.target_provider_id, mr.target_model,
                   p.provider_type, p.base_url, pk.key_encrypted
            FROM ai_model_route mr
            JOIN ai_provider p ON p.id = mr.target_provider_id
            JOIN ai_provider_key pk ON pk.provider_id = p.id AND pk.status = 'active'
            WHERE mr.status = 'active'
              AND ($1 ILIKE mr.model_pattern)
            ORDER BY mr.priority DESC, LENGTH(mr.model_pattern) DESC
            LIMIT 1
            """,
            model,
        )
        if not row:
            return None

        return RouteResult(
            target_provider_id=str(row["target_provider_id"]),
            target_model=row["target_model"],
            provider_type=row["provider_type"],
            base_url=row["base_url"],
            provider_key_encrypted=row["key_encrypted"],
        )
```

- [ ] **Step 2: Verify import**

```bash
python -c "from ai_gateway.repository import resolve_model_route; print('OK')"
```
Expected: `OK`

- [ ] **Step 3: Seed test data & verify**

```bash
python -c "
import asyncio, asyncpg
async def seed():
    conn = await asyncpg.connect(user='apihub', password='apihub_dev_pwd', database='apihub', host='127.0.0.1', port=5432)
    await conn.execute('DELETE FROM ai_model_route; DELETE FROM ai_provider_key; DELETE FROM ai_provider;')
    pid = await conn.fetchval(\"INSERT INTO ai_provider(name, provider_type, base_url, default_model) VALUES('test-openai','openai_compatible','https://api.openai.com/v1','gpt-4o-mini') RETURNING id\")
    await conn.execute('INSERT INTO ai_provider_key(provider_id, key_alias, key_encrypted, key_prefix) VALUES($1,$$default$$,$$test-enc$$,$$sk-test$$)', pid)
    await conn.execute(\"INSERT INTO ai_model_route(model_pattern, target_provider_id, target_model, priority) VALUES('gpt-4o*', $1, 'gpt-4o-mini', 10)\", pid)
    r = await conn.fetchrow('SELECT count(*) as cnt FROM ai_provider')
    print(f'providers: {r[\"cnt\"]}')
    r2 = await conn.fetchrow('SELECT count(*) as cnt FROM ai_model_route')
    print(f'routes: {r2[\"cnt\"]}')
    await conn.close()
asyncio.run(seed())
"
```
Expected: providers=1, routes=1

- [ ] **Step 4: Commit**

```bash
git add services/services/ai-gateway/src/ai_gateway/repository.py
git commit -m "feat(ai-gateway): 模型路由查询"
```

---

### Task 5: providers/ — OpenAI 兼容 + Anthropic Adapter

**Files:**
- Create: `services/services/ai-gateway/src/ai_gateway/providers/__init__.py`
- Create: `services/services/ai-gateway/src/ai_gateway/providers/openai_compat.py`
- Create: `services/services/ai-gateway/src/ai_gateway/providers/anthropic.py`

- [ ] **Step 1: Write `providers/__init__.py`**

```python
from collections.abc import AsyncIterator
from abc import ABC, abstractmethod

from ai_gateway.models import SSEChunk


class BaseProvider(ABC):
    @abstractmethod
    async def chat_completion(
        self,
        messages: list[dict],
        model: str,
        api_key: str,
        base_url: str,
        stream: bool = True,
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra_body: dict | None = None,
    ) -> AsyncIterator[SSEChunk]:
        ...


_PROVIDERS: dict[str, type[BaseProvider]] = {}


def register_provider(provider_type: str, cls: type[BaseProvider]) -> None:
    _PROVIDERS[provider_type] = cls


def get_provider(provider_type: str) -> BaseProvider:
    cls = _PROVIDERS.get(provider_type)
    if not cls:
        raise ValueError(f"Unsupported provider_type: {provider_type}")
    return cls()
```

- [ ] **Step 2: Write `providers/openai_compat.py`**

```python
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
        if temperature is not None: payload["temperature"] = temperature
        if max_tokens is not None: payload["max_tokens"] = max_tokens
        if extra_body: payload.update(extra_body)

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
                    if not line.startswith("data: "): continue
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
```

- [ ] **Step 3: Write `providers/anthropic.py`**

```python
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


def _map_stop_reason(reason: str | None) -> str | None:
    return {"end_turn": "stop", "max_tokens": "length", "stop_sequence": "stop"}.get(reason, reason)


class AnthropicProvider(BaseProvider):
    async def chat_completion(
        self, messages, model, api_key, base_url, stream=True,
        temperature=None, max_tokens=None, extra_body=None,
    ) -> AsyncIterator[SSEChunk]:
        url = f"{base_url.rstrip('/')}/v1/messages"
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        payload = {
            "model": model,
            "messages": _openai_to_anthropic_messages(messages),
            "max_tokens": max_tokens or 1024,
            "stream": stream,
        }
        if temperature is not None: payload["temperature"] = temperature
        if extra_body: payload.update(extra_body)

        async with httpx.AsyncClient(timeout=120.0) as client:
            if not stream:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                content = _extract_text(data.get("content", []))
                ud = data.get("usage", {})
                usage = {"prompt_tokens": ud.get("input_tokens",0), "completion_tokens": ud.get("output_tokens",0), "total_tokens": ud.get("input_tokens",0)+ud.get("output_tokens",0)}
                yield SSEChunk(content=content, finish_reason="stop", usage=usage)
                return

            async with client.stream("POST", url, json=payload, headers=headers) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if line.startswith("event: "): continue
                    if not line.startswith("data: "): continue
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
                        ud = data.get("usage", {})
                        usage = {"prompt_tokens": ud.get("input_tokens",0), "completion_tokens": ud.get("output_tokens",0), "total_tokens": ud.get("input_tokens",0)+ud.get("output_tokens",0)} if ud else None
                        yield SSEChunk(finish_reason=_map_stop_reason(data.get("stop_reason")), usage=usage)
                    elif et == "message_stop":
                        yield SSEChunk(finish_reason="stop")
```

- [ ] **Step 4: Register providers**

Append to `providers/__init__.py`:

```python
from ai_gateway.providers.openai_compat import OpenAICompatibleProvider
from ai_gateway.providers.anthropic import AnthropicProvider

register_provider("openai_compatible", OpenAICompatibleProvider)
register_provider("anthropic", AnthropicProvider)
```

- [ ] **Step 5: Verify registration**

```bash
python -c "
from ai_gateway.providers import get_provider
p1 = get_provider('openai_compatible')
p2 = get_provider('anthropic')
print(f'OK: {type(p1).__name__}, {type(p2).__name__}')
"
```
Expected: `OK: OpenAICompatibleProvider, AnthropicProvider`

- [ ] **Step 6: Commit**

```bash
git add services/services/ai-gateway/src/ai_gateway/providers/
git commit -m "feat(ai-gateway): Provider 抽象 + OpenAI + Anthropic adapters"
```

---

### Task 6: routes.py + main.py — FastAPI 端点

**Files:**
- Create: `services/services/ai-gateway/src/ai_gateway/routes.py`
- Create: `services/services/ai-gateway/src/ai_gateway/main.py`
- Create: `services/services/ai-gateway/src/ai_gateway/__init__.py`

- [ ] **Step 1: Write `routes.py`**

```python
import json
from collections.abc import AsyncIterator
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from ai_gateway.crypto import decrypt
from ai_gateway.models import ChatRequest, ChatResponse, SSEChunk
from ai_gateway.repository import resolve_model_route
from ai_gateway.providers import get_provider

router = APIRouter()


def _to_sse_line(chunk: SSEChunk) -> bytes:
    obj = {"id": "chatcmpl-ai-gateway", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": chunk.content} if chunk.content else {}, "finish_reason": chunk.finish_reason}]}
    if chunk.usage: obj["usage"] = chunk.usage
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8")


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
        chunk = await anext(provider_iter)
        return ChatResponse(
            choices=[{"index": 0, "message": {"role": "assistant", "content": chunk.content}, "finish_reason": "stop"}],
            usage=chunk.usage or {},
        )

    return StreamingResponse(
        _stream_sse(provider_iter),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no"},
    )


async def _stream_sse(provider_iter: AsyncIterator[SSEChunk]) -> AsyncIterator[bytes]:
    try:
        async for chunk in provider_iter:
            yield _to_sse_line(chunk)
            if chunk.finish_reason: break
    finally:
        yield b"data: [DONE]\n\n"
```

- [ ] **Step 2: Write `main.py`**

```python
from apihub_core import create_app
from ai_gateway.routes import router


def _build(app):
    app.include_router(router)


app = create_app(
    service_name="ai-gateway",
    build_routes=_build,
    skip_auth_paths=("/health", "/metrics", "/docs", "/openapi.json", "/v1/chat/completions"),
)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("ai_gateway.main:app", host="0.0.0.0", port=8013, workers=1, log_level="info")
```

- [ ] **Step 3: Write `__init__.py`**

```python
# ai-gateway
```

- [ ] **Step 4: Verify startup**

```bash
python -c "from ai_gateway.main import app; print(f'routes: {len(app.routes)}'); print('OK')"
```
Expected: routes > 0 / OK

- [ ] **Step 5: Commit**

```bash
git add services/services/ai-gateway/src/ai_gateway/routes.py services/services/ai-gateway/src/ai_gateway/main.py services/services/ai-gateway/src/ai_gateway/__init__.py
git commit -m "feat(ai-gateway): FastAPI 端点"
```

---

### Task 7: Makefile + Dockerfile

**Files:**
- Modify: `Makefile`
- Create: `services/services/ai-gateway/Dockerfile`

- [ ] **Step 1: Add Makefile target**

After `run-notification:` target, add:

```makefile
run-ai-gateway:  ## 本地启动 ai-gateway（LLM 推理路由，需要 PG）
	uvicorn ai_gateway.main:app --reload --port 8013
```

Also add `run-ai-gateway` to the help line (line 3):

```makefile
        run-registry run-dispatcher run-auth run-executor run-quota run-tenant run-admin run-docs run-trace run-retry run-workflow run-notification run-portal run-ai-gateway \
```

- [ ] **Step 2: Write `Dockerfile`**

```dockerfile
FROM python:3.14-slim AS builder
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends build-essential libpq-dev libffi-dev && rm -rf /var/lib/apt/lists/*
RUN useradd -m -u 1000 apihub && chown -R apihub:apihub /app
USER apihub
COPY --chown=apihub:apihub services/libs/apihub-core /tmp/apihub-core
RUN pip install --user /tmp/apihub-core
COPY --chown=apihub:apihub services/services/ai-gateway /tmp/ai-gateway
RUN pip install --user /tmp/ai-gateway
FROM python:3.14-slim
WORKDIR /app
COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH
EXPOSE 8013
CMD ["uvicorn", "ai_gateway.main:app", "--host", "0.0.0.0", "--port", "8013", "--workers", "1"]
```

- [ ] **Step 3: Verify**

```bash
make run-ai-gateway --just-print
```
Expected: `uvicorn ai_gateway.main:app --reload --port 8013`

- [ ] **Step 4: Commit**

```bash
git add Makefile services/services/ai-gateway/Dockerfile
git commit -m "chore(ai-gateway): Makefile + Dockerfile"
```

---

### Task 8: 单测

**Files:**
- Create: `services/services/ai-gateway/tests/__init__.py`
- Create: `services/services/ai-gateway/tests/conftest.py`
- Create: `services/services/ai-gateway/tests/test_providers.py`
- Create: `services/services/ai-gateway/tests/test_routes.py`

- [ ] **Step 1: Write `tests/conftest.py`**

```python
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def client():
    os.environ.setdefault("AI_GATEWAY_ENCRYPTION_KEY", "a" * 64)
    from apihub_core.config import get_settings; get_settings.cache_clear()
    from ai_gateway.main import app
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
```

- [ ] **Step 2: Write `tests/test_providers.py`**

```python
import pytest
from ai_gateway.models import SSEChunk
from ai_gateway.providers.openai_compat import OpenAICompatibleProvider
from ai_gateway.providers.anthropic import AnthropicProvider


class TestOpenAICompatibleProvider:
    @pytest.mark.asyncio
    async def test_non_stream(self, monkeypatch):
        async def mock_post(self, url, **kw):
            class FakeResp:
                async def raise_for_status(self): pass
                def json(self): return {"choices":[{"message":{"content":"Hello"}}], "usage":{"prompt_tokens":10,"completion_tokens":20,"total_tokens":30}}
            return FakeResp()
        monkeypatch.setattr("httpx.AsyncClient.post", mock_post)
        provider = OpenAICompatibleProvider()
        chunks = [c async for c in provider.chat_completion(messages=[{"role":"user","content":"Hi"}], model="gpt-4o-mini", api_key="sk-test", base_url="https://test.api.com/v1", stream=False)]
        assert chunks[0].content == "Hello"
        assert chunks[0].usage["total_tokens"] == 30

    @pytest.mark.asyncio
    async def test_stream(self, monkeypatch):
        async def mock_stream(self, method, url, **kw):
            class FakeResp:
                async def __aenter__(self): return self
                async def __aexit__(self, *a): pass
                async def raise_for_status(self): pass
                async def aiter_lines(self):
                    for l in ['data: {"choices":[{"delta":{"content":"Hello"},"index":0}]}', 'data: {"choices":[{"delta":{},"index":0,"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":20,"total_tokens":30}}', 'data: [DONE]']:
                        yield l
            return FakeResp()
        monkeypatch.setattr("httpx.AsyncClient.stream", mock_stream)
        provider = OpenAICompatibleProvider()
        chunks = [c async for c in provider.chat_completion(messages=[{"role":"user","content":"Hi"}], model="gpt-4o-mini", api_key="sk-test", base_url="https://test.api.com/v1", stream=True)]
        assert chunks[0].content == "Hello"
        assert chunks[-1].usage["total_tokens"] == 30


class TestAnthropicProvider:
    @pytest.mark.asyncio
    async def test_non_stream(self, monkeypatch):
        async def mock_post(self, url, **kw):
            class FakeResp:
                async def raise_for_status(self): pass
                def json(self): return {"content":[{"type":"text","text":"Hi"}],"usage":{"input_tokens":15,"output_tokens":25}}
            return FakeResp()
        monkeypatch.setattr("httpx.AsyncClient.post", mock_post)
        provider = AnthropicProvider()
        chunks = [c async for c in provider.chat_completion(messages=[{"role":"user","content":"Hello"}], model="claude-sonnet-4", api_key="sk-ant-test", base_url="https://api.anthropic.com", stream=False)]
        assert chunks[0].content == "Hi"
        assert chunks[0].usage["total_tokens"] == 40

    @pytest.mark.asyncio
    async def test_stream(self, monkeypatch):
        async def mock_stream(self, method, url, **kw):
            class FakeResp:
                async def __aenter__(self): return self
                async def __aexit__(self, *a): pass
                async def raise_for_status(self): pass
                async def aiter_lines(self):
                    for l in ['event: content_block_start', 'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
                              'event: content_block_delta', 'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello"}}',
                              'event: content_block_delta', 'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":" world"}}',
                              'event: message_delta', 'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"input_tokens":15,"output_tokens":10}}',
                              'event: message_stop', 'data: {"type":"message_stop"}']:
                        yield l
            return FakeResp()
        monkeypatch.setattr("httpx.AsyncClient.stream", mock_stream)
        provider = AnthropicProvider()
        chunks = [c async for c in provider.chat_completion(messages=[{"role":"user","content":"Hi"}], model="claude-sonnet-4", api_key="sk-ant-test", base_url="https://api.anthropic.com", stream=True)]
        assert any("Hello" in c.content for c in chunks)
        assert any("world" in c.content for c in chunks)
        usage_chunks = [c for c in chunks if c.usage]
        assert len(usage_chunks) >= 1
```

- [ ] **Step 3: Write `tests/test_routes.py`**

```python
import pytest
from ai_gateway.models import SSEChunk, RouteResult
from ai_gateway.crypto import encrypt


@pytest.mark.asyncio
async def test_model_not_found(client, monkeypatch):
    async def mock_resolve(model): return None
    monkeypatch.setattr("ai_gateway.routes.resolve_model_route", mock_resolve)
    resp = await client.post("/v1/chat/completions", json={"model":"unknown","messages":[{"role":"user","content":"Hi"}]})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_chat_completions_ok(client, monkeypatch):
    import os; os.environ["AI_GATEWAY_ENCRYPTION_KEY"] = "a"*64
    from apihub_core.config import get_settings; get_settings.cache_clear()
    test_key = encrypt("sk-test")
    route = RouteResult(target_provider_id="p1", target_model="gpt-4o-mini", provider_type="openai_compatible", base_url="https://test.com/v1", provider_key_encrypted=test_key)
    async def mock_resolve(model): return route
    monkeypatch.setattr("ai_gateway.routes.resolve_model_route", mock_resolve)
    async def mock_chat(*a, **kw):
        yield SSEChunk(content="ok", finish_reason="stop", usage={"total_tokens":10})
    monkeypatch.setattr("ai_gateway.routes.get_provider", lambda pt: type("M", (), {"chat_completion": mock_chat})())
    resp = await client.post("/v1/chat/completions", json={"model":"gpt-4o-mini","messages":[{"role":"user","content":"Hi"}],"stream":False})
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "ok"


@pytest.mark.asyncio
async def test_health(client):
    resp = await client.get("/health/live")
    assert resp.status_code == 200
```

- [ ] **Step 4: Run tests**

```bash
cd services/services/ai-gateway
python -m pytest tests/test_providers.py tests/test_routes.py -v
```
Expected: **ALL PASS**

- [ ] **Step 5: Commit**

```bash
git add services/services/ai-gateway/tests/
git commit -m "test(ai-gateway): Provider + 路由单测"
```

---

### Task 9: lint + final check

- [ ] **Step 1: Ruff**

```bash
ruff check services/services/ai-gateway/
```
Expected: zero errors

- [ ] **Step 2: Mypy**

```bash
mypy services/services/ai-gateway/
```
Expected: zero errors

- [ ] **Step 3: Verify existing lint not broken**

```bash
ruff check services/libs/apihub-core/ services/services/ --quiet
```
Expected: quiet (no output)

- [ ] **Step 4: Final commit if needed**
