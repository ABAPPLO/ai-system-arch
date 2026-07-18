# dispatcher-sse-status — SSE 上游 status 透传 + forwarder 测试（design）

**日期**：2026-07-18
**分支**：`fix/dispatcher-sse-status`（base main = `7b1dd2e`，R2c 合后）
**来源**：R2c final review（opus）Deferred K1 —— dispatcher `_forward_stream` 不透传上游 status_code。

## 背景

dispatcher `_forward_stream`（`forwarder.py:114-163`）在 `stream_and_emit()` generator 迭代**前**构造 `StreamingResponse(status_code=200)`，上游 `resp.status_code` 仅在 generator 内（lazy，L132）才赋值给闭包变量 → **SSE 路径上游 4xx/5xx 对 client 表现 HTTP 200**（错误 body 仍透传，仅 status 丢）。pre-existing 自原 dispatcher `a8d8753`，非 R2c/R2e 引入。`_forward_sync` 路径正常透传（`forwarder.py:109`）。影响所有 AI 消费者错误处理（HTTP 200 on AI 4xx/5xx 破坏标准错误处理）。

## fix（`_forward_stream` 改 eager-open）

1. `cm = self._client.stream(...)`；`upstream = await cm.__aenter__()`（连接建立，拿到 resp）。
2. `status_code = upstream.status_code`（在构造 `StreamingResponse` **前**捕获）。
3. 连接建立失败（`__aenter__` 抛 `httpx.RequestError`）→ `raise ApiError(ErrorCode.API_DOWN, ..., http_status=503)`（同 `_forward_sync` 的 backend unreachable 路径）+ `_emit_failure`。
4. `StreamingResponse(stream_and_emit(), status_code=status_code, ...)`；`stream_and_emit` 用已 open 的 `upstream` 迭代（`upstream.aiter_bytes()`），`finally` 关 `await cm.__aexit__(None,None,None)`。
5. mid-stream 迭代失败（`aiter_bytes` 抛 `httpx.RequestError`）→ yield error event（`data: {"error":"backend error"}\n\n`，保留当前行为）；`_emit_stream_complete` 用**上游真 `status_code`**（billing 按上游响应，不强行 503——上游真返 200 时迭代中断是交付问题，非上游错）。

## 测试（补 `tests/test_forwarder.py`，当前零覆盖）

`async_client` fixture（httpx ASGITransport + monkeypatch auth）+ monkeypatch `httpx.AsyncClient`/`httpx.AsyncClient.stream` mock 上游。覆盖：
- **stream 正常 200**：上游返 SSE chunks + 末尾 `usage` → client 收 **200** + chunks + `[DONE]`；emit 记 token_prompt/completion。
- **stream 上游 4xx**（核心回归）：上游返 404/400 → **client 收 4xx**（非 200）。
- **stream 上游 5xx**：上游返 500 → client 收 500。
- **stream 连接失败**：`__aenter__` 抛 `RequestError` → `ApiError` 503。
- **stream mid-stream 失败**：上游先 200 后 `aiter_bytes` 断 → client 收 200 + error event；emit 用 200。
- **sync 正常 + 错误**（顺带覆盖 `_forward_sync`：200 透传 + 上游 5xx 透传 + 连接失败 503）。

## 范围 / 非范围

**范围**：`forwarder.py::_forward_stream` + 新 `tests/test_forwarder.py`。
**非范围**：`_forward_sync` 主干（不改，仅补测试）、`_emit*` 函数、`_extract_tokens_from_chunk`、dispatcher 其他模块。

## 风险

- eager-open 改变连接生命周期（`cm` 须在 finally 关）——测试覆盖连接失败 + mid-stream 路径。
- mid-stream emit 用上游 status（200）vs 当前 503：语义变化（billing 按上游响应）—— 可接受（上游真返 200，迭代中断是交付问题）。
- `_forward_stream` 零现有测试 → 新测试是回归网，必须先写 4xx 红（证明 bug）再 fix 绿（TDD）。
