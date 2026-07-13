# Phase 4 第一切片「AI 网关」设计

> 日期：2026-07-13
> 阶段：Phase 4 演进 — AI 网关（LLM 统一接入 / 流式响应 / Token 计费）
> 关联 ADR：ADR-004（AI 网关扩展，已预留全部扩展点）
> 关联文档：`docs/05-core-flows.md`、`docs/03-services.md`、`docs/04-data-model.md`

## 1. Goal

在现有 dispatcher SSE 透传 + Token 计量基础上，构建完整的 LLM 推理路由层（AI Gateway），让平台可以统一接入多个 LLM Provider，外部调用方用 OpenAI 兼容 SDK 即可调用不同模型。

### 1.1 已有基础（已实现）

| 组件 | 状态 | 说明 |
|------|------|------|
| `backend_type: ai_model` | ✅ | api_version 枚举，ADR-004 预留 |
| `ai_model` + `ai_streaming` 字段 | ✅ | schema/模型已支持 |
| dispatcher SSE 流式转发 | ✅ | `_forward_stream()` via httpx StreamingResponse |
| dispatcher Token usage 采集 | ✅ | `_extract_tokens_from_chunk()` from SSE |
| Kafka api-call-events token 字段 | ✅ | `token_prompt/completion/total` 已在 event 模型 |
| schema billing.type=token | ✅ | llm-chat.yaml 示例含 Token 计费 tier |

### 1.2 本切片做

- **ai-gateway 新服务**：独立 FastAPI 服务，处理模型路由、Provider 适配、Key 管理
- **Provider 插件框架**：BaseProvider 抽象 + OpenAI 兼容 + Anthropic adapter
- **PG 配置三张表**：`ai_provider` / `ai_provider_key` / `ai_model_route`
- **OpenAI 兼容 API**：`POST /v1/chat/completions`，调用方可用现有 OpenAI SDK
- **计费集成**：ai-gateway 输出 OpenAI 兼容 SSE（含 usage），**dispatcher 现有 `_forward_stream` + `_extract_tokens_from_chunk` 自动完成 Token 用量上报**，ai-gateway 不碰 Kafka

### 1.3 非目标

- Provider 间 failover/fallback（先单 provider，后续可以加）
- Prompt 缓存/语义缓存（后续优化）
- 多模态（图片/音频输入——gateway 架构已支持，模型限制）
- AI 请求的完整审计日志（event 现有的 trace_id 可用，独立审计走 Phase 4 后续）
- Provider 健康检查和自动切换（后续迭代）

### 1.4 成功标准

- ✅ 注册一个 `openai_compatible` provider → 能通过 ai-gateway 调通
- ✅ 注册一个 `anthropic` provider → 能通过 ai-gateway 调通
- ✅ 模型路由 `model_pattern` 正确匹配 → 转发到对应 provider
- ✅ 流式 SSE 全程正常（OpenAI/Anthropic 都转成 OpenAI 格式流回）
- ✅ Token 用量随 SSE 带回，dispatcher 正常提取并上报（复用现有逻辑）
- ✅ Provider API Key AES-256 加解密正常
- ✅ `ruff check` + `mypy` clean
- ✅ 现有 smoke 不破

## 2. 架构总览

```
┌─ 调用方 ──────────────────────────────────────────────────────┐
│  POST /v1/chat/completions                                    │
│  Authorization: Bearer <apihub_api_key>                        │
│  {"model": "gpt-4o-mini", "messages": [...], "stream": true}   │
└──────────────────────────┬────────────────────────────────────┘
                           │ APISIX → dispatcher
                           ▼
┌──────────────────────────────────────────────────────────────┐
│                    dispatcher (现有逻辑，不改动)               │
│                                                              │
│  api.backend_type = ai_model                                 │
│  backend_url = "http://ai-gateway:8013/v1/chat/completions"   │
│  → httpx 透传请求体 + SSE 流式转发（现有 forward 逻辑）       │
└──────────────────────────┬────────────────────────────────────┘
                           │ 内部 HTTP
                           ▼
┌──────────────────────────────────────────────────────────────┐
│                    ai-gateway (:8013)                         │
│                                                              │
│  POST /v1/chat/completions                                    │
│                                                              │
│  ① 解析请求体 → model = "gpt-4o-mini"                       │
│  ② ai_model_route: "gpt-4o*" → provider=openai-main         │
│  ③ ai_provider: base_url, provider_type                      │
│  ④ ai_provider_key: 解密 → sk-...                           │
│  ⑤ Provider Adapter 构造上游请求                              │
│  ⑥ httpx → 上游 LLM API                                      │
│  ⑦ 流式转码 (Anthropic SSE → OpenAI SSE，含 usage)          │
│  ⑧ SSE 流回 dispatcher → dispatcher 现有逻辑提取 token 上报  │
└──────┬───────────────────────────────────────────────────────┘
       │                      │                    │
       ▼                      ▼                    ▼
┌──────────────┐  ┌────────────────┐  ┌──────────────────────┐
│ OpenAI       │  │ Anthropic      │  │ 通义千问/DeepSeek     │
│ (直接透传)    │  │ (SSE 转码)     │  │ (OpenAI 兼容直传)     │
└──────────────┘  └────────────────┘  └──────────────────────┘
```

### 2.1 集成要点

- **dispatcher 不需要改代码**：ai-gateway 只是一个新的 `backend_url` 目标，现有 SSE 透传逻辑完整可用
- **API 形状**：ai-gateway 暴露 OpenAI 兼容 `POST /v1/chat/completions`
- **流式统一**：所有 Provider 的 SSE 格式在 gateway 层转成 OpenAI SSE 格式，调用方始终看到一致的流式格式
- **端口**：8012（registry=8000, dispatcher=8001, auth=8002, executor=8003, quota=8004…）

### 2.2 计费集成说明（无需改 dispatcher）

ai-gateway **不直接推 Kafka**。计费数据流：

```
ai-gateway SSE 流回（OpenAI 格式，最终 chunk 含 usage）
    → dispatcher _forward_stream() 接收
    → _extract_tokens_from_chunk() 从 SSE 提取 token 数
    → _emit_stream_complete() 推 Kafka api-call-events（含 token_prompt/completion/total）
    → ClickHouse 消费（现有链路）
```

因为所有 Provider 的输出在 ai-gateway 层被归一化为 OpenAI SSE 格式（含 `usage` 字段），dispatcher 现有的 token 提取逻辑无需修改即可工作。ai-gateway 不需要 Kafka 依赖。

## 3. 数据模型

新增 3 张 PG 表，全量加 RLS（`SET LOCAL app.tenant_id`），遵循现有模型命名惯例。

### 3.1 `ai_provider` — Provider 配置

```sql
CREATE TABLE ai_provider (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name          TEXT NOT NULL UNIQUE,    -- 唯一名称，如 "openai-main"
    provider_type TEXT NOT NULL,           -- "openai_compatible" | "anthropic"
    base_url      TEXT NOT NULL,           -- 上游 API 地址
    default_model TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

| 字段 | 示例 |
|------|------|
| `name` | `openai-main` |
| `provider_type` | `openai_compatible` |
| `base_url` | `https://api.openai.com/v1` 或 `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| `default_model` | `gpt-4o-mini` |

> `ai_provider` 和 `ai_model_route` 是平台基础设施配置，不按租户隔离，`admin_db_session()` 读写。
> 租户级别的 API 可见性由 dispatcher 在 API 层控制（已有逻辑），gateway 不接触 API 元数据。

### 3.2 `ai_provider_key` — 加密 Provider API Key

```sql
CREATE TABLE ai_provider_key (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    provider_id    UUID NOT NULL REFERENCES ai_provider(id) ON DELETE CASCADE,
    key_alias      TEXT NOT NULL DEFAULT '',   -- 如 "default" / "backup"
    key_encrypted  TEXT NOT NULL,              -- AES-256-GCM 加密
    key_prefix     TEXT NOT NULL DEFAULT '',   -- 明文前 8 位，如 "sk-abc..."
    expires_at     TIMESTAMPTZ,
    status         TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','expired','revoked')),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

**加密方案：**

```
provider API Key 明文 → AES-256-GCM 加密 → 密文存 key_encrypted
解密：读 key_encrypted → AES-256-GCM 解密 → 明文（仅内存，不落日志）
密钥来源：环境变量 AI_GATEWAY_ENCRYPTION_KEY（32 字节 hex）
```

**为什么分层两表？** 一个 provider 可能有多个 key（负载均衡 / 轮换 / 不同额度），分离后支持多 key 管理且 key 加解密不影响 provider 配置查询。

### 3.3 `ai_model_route` — 模型路由表

```sql
CREATE TABLE ai_model_route (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    model_pattern      TEXT NOT NULL,         -- "gpt-4o*"、"claude-*"、"qwen-*"
    target_provider_id UUID NOT NULL REFERENCES ai_provider(id),
    target_model       TEXT NOT NULL,         -- 转成 provider 的模型名
    priority           INT NOT NULL DEFAULT 0,
    status             TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled')),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

**路由匹配逻辑：**

```
请求体 model = "gpt-4o-mini"
→ 查询：
    SELECT mr.*, p.provider_type, p.base_url
    FROM ai_model_route mr
    JOIN ai_provider p ON p.id = mr.target_provider_id
    WHERE mr.status = 'active'
      AND ($1 ILIKE mr.model_pattern)
    ORDER BY mr.priority DESC, LENGTH(mr.model_pattern) DESC
    LIMIT 1
→ 匹配到 → 得 target_provider_id + target_model + base_url
→ 未匹配 → 400 {error: "model xxx not supported"}
```

支持模式：`gpt-4o*`、`claude-*`、`gpt-*`、`qwen-*`。最具体的匹配胜出。

## 4. Provider 抽象层

### 4.1 BaseProvider 接口

```python
@dataclass
class SSEChunk:
    """统一流式 chunk 格式（OpenAI SSE shape）。"""
    content: str = ""
    finish_reason: str | None = None
    usage: dict | None = None   # 结束 chunk 携带 token 用量


class BaseProvider(ABC):
    """所有 Provider 需实现的接口。"""

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
        """聊天补全。即使非流式也返回 AsyncIterator（只含一个 chunk）。"""
        ...
```

### 4.2 OpenAICompatibleProvider

```python
class OpenAICompatibleProvider(BaseProvider):
    """覆盖 OpenAI + 通义千问 + DeepSeek 等格式兼容的 provider。"""

    async def chat_completion(self, ...) -> AsyncIterator[SSEChunk]:
        url = f"{base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {"model": model, "messages": messages, "stream": stream}
        if temperature is not None: payload["temperature"] = temperature
        if max_tokens is not None:  payload["max_tokens"] = max_tokens
        if extra_body:              payload.update(extra_body)

        if not stream:
            resp = await client.post(url, json=payload, headers=headers)
            data = resp.json()
            usage = data.get("usage", {})
            content = data["choices"][0]["message"]["content"]
            yield SSEChunk(content=content, finish_reason="stop", usage=usage)
            return

        async with client.stream("POST", url, json=payload, headers=headers) as resp:
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[6:].strip()
                if data_str == "[DONE]":
                    yield SSEChunk(finish_reason="stop")
                    return
                data = json.loads(data_str)
                choice = data["choices"][0]
                delta = choice.get("delta", {})
                yield SSEChunk(
                    content=delta.get("content", ""),
                    finish_reason=choice.get("finish_reason"),
                    usage=data.get("usage"),
                )
```

### 4.3 AnthropicProvider

```python
class AnthropicProvider(BaseProvider):
    """Anthropic Messages API adapter — 请求/响应双向转码。"""

    async def chat_completion(self, ...) -> AsyncIterator[SSEChunk]:
        url = f"{base_url}/v1/messages"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": model,
            "messages": _openai_to_anthropic_messages(messages),
            "max_tokens": max_tokens or 1024,
            "stream": stream,
        }

        if not stream:
            resp = await client.post(url, json=payload, headers=headers)
            data = resp.json()
            content = _extract_text(data["content"])
            usage = {
                "prompt_tokens": data["usage"]["input_tokens"],
                "completion_tokens": data["usage"]["output_tokens"],
                "total_tokens": data["usage"]["input_tokens"] + data["usage"]["output_tokens"],
            }
            yield SSEChunk(content=content, finish_reason="stop", usage=usage)
            return

        async with client.stream("POST", url, json=payload, headers=headers) as resp:
            async for line in resp.aiter_lines():
                if line.startswith("event: "):
                    continue
                if not line.startswith("data: "):
                    continue
                data = json.loads(line[6:])
                event_type = data.get("type", "")

                if event_type == "content_block_delta":
                    delta = data.get("delta", {})
                    if delta.get("type") == "text_delta":
                        yield SSEChunk(content=delta.get("text", ""))
                elif event_type == "message_delta":
                    usage = data.get("usage", {})
                    yield SSEChunk(
                        finish_reason=_map_stop_reason(data.get("stop_reason")),
                        usage={
                            "prompt_tokens": usage.get("input_tokens", 0),
                            "completion_tokens": usage.get("output_tokens", 0),
                            "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
                        } if usage else None,
                    )
                elif event_type == "message_stop":
                    yield SSEChunk(finish_reason="stop")
```

### 4.4 Provider 注册

```python
_PROVIDERS: dict[str, type[BaseProvider]] = {}

def register_provider(provider_type: str, cls: type[BaseProvider]):
    _PROVIDERS[provider_type] = cls

def get_provider(provider_type: str) -> BaseProvider:
    cls = _PROVIDERS.get(provider_type)
    if not cls:
        raise ValueError(f"Unsupported provider_type: {provider_type}")
    return cls()

# 初始化
register_provider("openai_compatible", OpenAICompatibleProvider)
register_provider("anthropic", AnthropicProvider)
```

## 5. 请求流详细说明

### 5.1 路由端点

```
POST /v1/chat/completions
→ ① 解析请求体
→ ② repository.resolve_model_route(model) — 查 PG 路由表
→ ③ 未匹配 → 400 "model not supported"
→ ④ crypto.decrypt(provider_key) — 解密
→ ⑤ get_provider(provider_type).chat_completion(...) — 调上游
→ ⑥ stream=True → StreamingResponse（透传 SSE 流回 dispatcher）
→ ⑦ stream=False → 聚合一次返回
→ ⑧ SSE 最终 chunk 携带 usage → dispatcher 现有 _forward_stream 自动提取并上报
```

### 5.2 计费流（dispatcher 侧已有逻辑）

ai-gateway 的 SSE 输出中，最终 chunk 携带 OpenAI 格式的 `usage`：

```
data: {"id":"...","choices":[{"delta":{},"index":0,"finish_reason":"stop"}],"usage":{"prompt_tokens":150,"completion_tokens":320,"total_tokens":470}}\n\n
data: [DONE]\n\n
```

dispatcher `_forward_stream()` → `_extract_tokens_from_chunk()` 解析 `usage` → `_emit_stream_complete()` 推 Kafka。ai-gateway 不需要 Kafka 依赖。

### 5.3 SSE 行格式（OpenAI 兼容）

```
data: {"id":"...","object":"chat.completion.chunk","choices":[{"delta":{"content":"Hello"},"index":0}]}\n\n
data: {"id":"...","object":"chat.completion.chunk","choices":[{"delta":{"content":" world"},"index":0}]}\n\n
data: [DONE]\n\n
```

## 6. 配置项

```python
# apihub-core config.py 新增
ai_gateway_encryption_key: str = ""   # AES-256 密钥（32 字节 hex），必填
```

## 7. 文件清单

### 7.1 新文件（11 个）

```
services/services/ai-gateway/
├── pyproject.toml
├── src/
│   └── ai_gateway/
│       ├── __init__.py
│       ├── main.py                   # FastAPI 入口 + lifespan
│       ├── routes.py                 # POST /v1/chat/completions
│       ├── models.py                 # Pydantic 模型 + PG 表定义
│       ├── repository.py             # 路由查询 / key 解密
│       ├── crypto.py                 # AES-256-GCM 加解密
│       └── providers/
│           ├── __init__.py            # BaseProvider + registry
│           ├── openai_compat.py      # OpenAI 兼容 adapter
│           └── anthropic.py          # Anthropic adapter
└── tests/
    ├── __init__.py
    ├── conftest.py
    ├── test_routes.py
    └── test_providers.py
```

### 7.2 改动的文件（5 个）

| 文件 | 改动 |
|------|------|
| `services/libs/apihub-core/src/apihub_core/config.py` | 加 `ai_gateway_encryption_key` |
| `Makefile` | 加 `run-ai-gateway` target |
| `docker-compose.dev.yml` | 加 ai-gateway service |
| `deploy/k8s/overlays/dev/kustomization.yaml` | 加 ai-gateway Deployment |

## 8. 实现顺序

| # | 任务 | 文件 |
|---|------|------|
| 1 | crypto.py — AES-256-GCM 加解密 | 1 文件 |
| 2 | models.py — PG 表 + Pydantic 模型 | 1 文件 |
| 3 | repository.py — 路由查询 + key 解密 | 1 文件 |
| 4 | providers/openai_compat.py | 1 文件 |
| 5 | providers/anthropic.py | 1 文件 |
| 6 | routes.py + main.py | 2 文件 |
| 7 | 配置 + Docker + Makefile | 3-4 文件 |
| 8 | 单测 | 3 文件 |
| 9 | ruff check + mypy | — |

## 9. 风险

| 风险 | 影响 | 对策 |
|------|------|------|
| Provider API Key 泄露 | 高 | AES-256-GCM 加密存储，只解密到内存；日志脱敏 |
| Anthropic SSE 格式变化 | 中 | adapter 层隔离，格式变化只改 anthropic.py |
| 通义千问等国内模型非完整 OpenAI 兼容 | 中 | openai_compatible provider 实测适配 |
| 流式过程中调用方断连 | 低 | dispatcher 感知断开 → finally 块上报已累计 token |
| 高并发下 Key 解密性能 | 低 | 解密在内存，单次 < 1ms，可加本地 LRU 缓存 |

## 10. 设计决策记录

| 决策 | 选项 | 选择 |
|------|------|------|
| 支持的 Provider | A-仅OpenAI / B-OpenAI+Anthropic / C-插件化 | **C** — 插件化，初始 2 个 |
| 模型路由策略 | A-API固定 / B-请求体覆盖 / C-白名单 | **B** — 请求体 model 动态路由 |
| Provider Key 存储 | A-环境变量 / B-数据库加密 / C-外部KMS | **B** — PG AES-256-GCM 加密 |
| 架构风格 | A-独立服务 / B-dispatcher内置 / C-混合 | **A** — 独立 ai-gateway 服务 |
| API 形状 | A-OpenAI 兼容 / B-自定格式 | **A** — OpenAI 兼容格式 |
