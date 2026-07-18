# R2c — ai-gateway 接入 dispatcher 成唯一 AI 流式入口（design）

**日期**：2026-07-18
**分支**：`fix/r2c-ai-gateway-dispatcher`（base main = `fc7dd77`，R2e 合后）
**audit 引用**：fix-program §3.4 —— ai-gateway 接入 dispatcher 成唯一 AI 流式入口（统一 token 计费/限流/多 Provider）。量=大，用户选一轮做完。

## 背景

Phase 4 部署了 ai-gateway（多 Provider 路由 + key 解密 + `/v1/chat/completions` SSE），但**未接入 dispatcher**：AI api（`schema/ai-service/llm-chat.yaml`）的 `backend_url` 是占位 `http://llm-gateway.internal/v1/chat/completions`（不存在的主机），dispatcher 转发到该占位 → AI 流量实际不通；且 ai-gateway 的 `/v1/chat/completions` 独立无人调，**绕过 dispatcher 的统一 token 计费/限流/脱敏/审计**。

R2c 让 ai-gateway 接入 dispatcher，使 dispatcher 成唯一 AI 流式入口。

## 现状（探索确认）

- **dispatcher**（`forwarder.py` `HttpForwarder`）已具备完整 ai_model 能力：SSE 流式转发（`_forward_stream`）+ token 提取（`_extract_tokens_from_chunk`，OpenAI 风格 `usage`）+ Kafka emit 计费（`_emit_stream_complete` 的 `tokens_prompt/completion`）+ 脱敏 + 经 APISIX（R1c 唯一路由层 + R1d key-auth/limit-count）。
- **ai-gateway**：`/v1/chat/completions` 多 Provider 路由（`resolve_model_route`）+ key 解密（`decrypt`）+ provider SSE（`_to_sse_line` 输出 `usage: {prompt_tokens, completion_tokens, total_tokens}`）。Service 已 **ClusterIP 内网**（`deploy/k8s/services/ai-gateway/deployment.yaml` Service 段，`port 80 → targetPort http`）+ `/v1/chat/completions` 在 `skip_auth_paths`（`main.py`）→ dispatcher 内网可直调，无需 X-API-Key。
- **关键差距**：AI api `backend_url` = 占位 `llm-gateway.internal`，未指向 ai-gateway 服务。
- **能力已具备（R2c 不重造）**：
  - dispatcher token 提取与 ai-gateway SSE `usage` 格式兼容（均为 OpenAI 风格）。
  - ai-gateway 不 emit Kafka → 计费由 dispatcher 单向加（拓扑走通即自动计费）。
  - ai-gateway Service/鉴权已 ClusterIP + skip_auth，无需改。

## 设计（拓扑 A：ai-gateway 作 dispatcher 内网 backend）

```
client → APISIX(key-auth + limit-count) → dispatcher(ai_model SSE 转发 + token 提取 + Kafka 计费 + 脱敏) → ai-gateway(ClusterIP, /v1/chat/completions skip_auth, 多 Provider 路由 + key 解密) → provider
```

dispatcher 统一入口（计费/限流/脱敏/审计），ai-gateway 聚焦多 Provider 路由 + key 解密（Phase 4 能力保留）。client 不再直达 ai-gateway（ClusterIP 不对外）。

## 改动

### 1. AI api `backend_url` 指向 ai-gateway
`schema/ai-service/llm-chat.yaml`：`backend_url: http://llm-gateway.internal/v1/chat/completions` → `http://ai-gateway.apihub-system/v1/chat/completions`。
（dispatcher resolver 从 DB `api_version.backend_url` 读转发目标；改 schema + apply 生效。）

### 2. seed/apply 更新 backend_url
经 `apihub-apply`（schema → change-request → apply）或 init-db/seed 把 llm-chat api 的 backend_url 更新到 DB。

### 3. ai_provider / ai_model_route seed（e2e mock provider）
- e2e 用 mock provider：`ai_provider.base_url = http://mock-backend.apihub-system:80/v1`（openai_compat provider 自拼 `/chat/completions`；mock-backend 返 OpenAI 风格 SSE 含 `usage`）。
- `ai_provider` + `ai_provider_key`（mock key，加密）seed。
- **不改 ai-gateway provider 代码**：`openai_compat` 直连 `base_url`，mock-backend 兼容 OpenAI SSE 即可。

### 4. e2e（kind）
client → APISIX → dispatcher → ai-gateway → mock-backend，断言：
- SSE 流式透传（client 收到 chunks + `[DONE]`）。
- token 计费 emit（Kafka `api-call-events` / ClickHouse `token_prompt/completion` 非零）。
- 限流（limit-count 超限 → 429）。
- ai-gateway 多 Provider 路由生效（`model` → route → mock provider，model 不匹配 → 400）。

### 5. 小修（仅 e2e 暴露才做）
- mock-backend 缺 SSE 端点则补一个（OpenAI 风格 `/v1/chat/completions` 流式 + 末尾 `usage`）。
- dispatcher → ai-gateway header 兼容（若 ai-gateway 对某 header 敏感）。
- ai-gateway 错误透传（provider 4xx/5xx → client）。

## 非范围

- ai-gateway 多 Provider 路由本身（Phase 4 已做，R2c 不改其路由/解密逻辑）。
- dispatcher forwarder AI 能力（已有，R2c 不改 forwarder 主干）。
- ai-gateway Service/鉴权（已 ClusterIP + skip_auth）。
- real provider 接入（e2e 用 mock；real key 部署期配）。

## 测试

- **e2e（kind）为主**：全链打通 + 计费 + 限流断言（复用 R2d/R2e 的 kind e2e 模式 + APISIX dispatcher 数据面）。
- dispatcher / ai-gateway 现有单测不回归。
- ruff/mypy clean（改动文件）。

## 风险

- mock-backend SSE 端点若不存在需补（影响 e2e 工作量，plan 确认）。
- APISIX 路由：llm-chat api 要 publish 到 APISIX（R1c）；e2e 依赖该路由 + consumer key。
- `ai_model_route` / `ai_provider` seed 要正确：ai-gateway `resolve_model_route` 找不到 model → 400（e2e seed 要匹配请求 model）。
- dispatcher → ai-gateway：dispatcher 透传 client body（含 `model/messages`），ai-gateway 用 `payload.model` 路由 → 兼容（plan 验证）。
