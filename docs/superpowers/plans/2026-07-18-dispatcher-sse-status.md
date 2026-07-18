# dispatcher-sse-status Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`).

**Goal:** `_forward_stream` eager-open 透传上游 status_code（修 SSE 路径 4xx/5xx→200 bug）+ 补 `test_forwarder.py`（当前零覆盖）。

**Architecture:** eager `cm.__aenter__()` 在构造 `StreamingResponse` 前捕获 `upstream.status_code`；连接失败 → `ApiError 503`（同 `_forward_sync`）；mid-stream 失败 → yield error event + emit 用上游真 status。

**Tech Stack:** Python 3.11, FastAPI, httpx, pytest（asyncio_mode=auto）。

**Spec:** `docs/superpowers/specs/2026-07-18-dispatcher-sse-status-design.md`

## Global Constraints

- **TDD**：先写 4xx 红测试（证明 bug：client 收 200 非 4xx）→ fix → 绿。
- python/pytest 走 repo-root `.venv/bin/python -m pytest services/services/dispatcher/tests/`（NOT services/.venv）。
- **不改**：`_forward_sync` 主干（仅补测试）、`_emit*` 函数、`_extract_tokens_from_chunk`、dispatcher 其他模块。
- dispatcher 测试用 `conftest.py` 的 `async_client` fixture（httpx ASGITransport + monkeypatch auth）。
- GateGuard：每文件首次 bash/edit 拦，报 facts retry。
- 每任务 commit。

---

### Task 1: _forward_stream eager-open + test_forwarder.py（TDD）

**Files:**
- Modify: `services/services/dispatcher/src/dispatcher/forwarder.py::_forward_stream`（约 L114-163）
- Create: `services/services/dispatcher/tests/test_forwarder.py`

**Interfaces:**
- Consumes: `HttpForwarder._client`（`httpx.AsyncClient`），`_emit_stream_complete`/`_emit_failure`/`_extract_tokens_from_chunk`（不改）。
- Produces: `_forward_stream` 透传上游 status_code；`StreamingResponse(status_code=upstream.status_code)`。

- [ ] **Step 1: 写 test_forwarder.py（TDD，先红）**

mock `HttpForwarder._client` 为 fake async client。核心红测试（证明 bug）：
```python
import pytest

class _FakeResp:
    def __init__(self, status_code, chunks=None, stream_error=None):
        self.status_code = status_code; self._chunks = chunks or []; self._stream_error = stream_error
    async def aiter_bytes(self):
        for c in self._chunks: yield c
        if self._stream_error: raise self._stream_error

class _FakeStreamCM:
    def __init__(self, resp_or_exc):
        self._v = resp_or_exc
    async def __aenter__(self):
        if isinstance(self._v, Exception): raise self._v
        return self._v
    async def __aexit__(self, *a): return False

@pytest.mark.asyncio
async def test_stream_upstream_4xx_propagated(monkeypatch, async_client):
    """核心回归：上游 4xx → client 收 4xx（当前 bug：收 200）。"""
    from dispatcher.forwarder import get_forwarder
    fwd = get_forwarder()
    async def _fake_stream(*a, **kw): return _FakeStreamCM(_FakeResp(404, chunks=[b'data: {"error":"not found"}\n\n']))
    monkeypatch.setattr(fwd._client, "stream", _fake_stream)
    # monkeypatch resolver 返一个 ai_streaming=True 的 snap（绕 DB），按 test_resolver.py 模式
    # POST 经 dispatcher 转发路径 → 断言 client 收 404
    resp = await async_client.post("<dispatch path>", ...)
    assert resp.status_code == 404, f"SSE 上游 4xx 应透传，got {resp.status_code}"
```
其他测试：`test_stream_normal_200`（chunks+usage→200+token emit）、`test_stream_5xx`、`test_stream_conn_fail_503`（`__aenter__` 抛 `httpx.ConnectError` → 503）、`test_stream_midstream_fail`（先 200 chunk 后 `aiter_bytes` 抛 → 200 + error event）、`test_sync_normal`/`test_sync_error`（顺带 `_forward_sync`）。具体 dispatch endpoint + snap seed 按 dispatcher routes + `test_resolver.py` monkeypatch 模式。

- [ ] **Step 2: 跑红**

```bash
.venv/bin/python -m pytest services/services/dispatcher/tests/test_forwarder.py::test_stream_upstream_4xx_propagated -v
```
Expected: FAIL（`assert 200 == 404`——当前 SSE status 不透传）。

- [ ] **Step 3: fix `_forward_stream`（eager-open）**

替换 `_forward_stream`（forwarder.py 约 L114-163）为：
```python
    async def _forward_stream(self, snap, request, url, headers, body, request_id, start):
        """AI SSE 流式 —— eager-open 上游捕获 status_code，chunk-by-chunk 转发 + 末尾 emit token。"""

        try:
            cm = self._client.stream(
                method=request.method, url=url, headers=headers,
                params=request.query_params, content=body, timeout=None,
            )
            upstream = await cm.__aenter__()
        except httpx.RequestError as e:
            backend_latency_ms = int((time.perf_counter() - start) * 1000)
            await _emit_failure(snap, request, e, request_id, backend_latency_ms)
            raise ApiError(
                ErrorCode.API_DOWN, f"backend unreachable: {e}", http_status=503,
            ) from e

        status_code = upstream.status_code

        async def stream_and_emit() -> AsyncIterator[bytes]:
            tokens_prompt = 0
            tokens_completion = 0
            total_bytes = 0
            try:
                async for chunk in upstream.aiter_bytes():
                    total_bytes += len(chunk)
                    p, c = _extract_tokens_from_chunk(chunk)
                    tokens_prompt += p
                    tokens_completion += c
                    yield chunk
            except httpx.RequestError as e:
                log.warning("stream_backend_error", error=str(e))
                yield b'data: {"error":"backend error"}\n\n'
            finally:
                latency_ms = int((time.perf_counter() - start) * 1000)
                await _emit_stream_complete(
                    snap, request, status_code, len(body), total_bytes,
                    latency_ms, request_id,
                    tokens_prompt=tokens_prompt, tokens_completion=tokens_completion,
                )
                with suppress(Exception):
                    await cm.__aexit__(None, None, None)

        return StreamingResponse(
            stream_and_emit(),
            media_type="text/event-stream",
            status_code=status_code,
            headers={"X-Accel-Buffering": "no"},
        )
```
（顶部 `from contextlib import suppress` 若未导入则加。）

- [ ] **Step 4: 跑绿 + 全 dispatcher 测试不回归**

```bash
.venv/bin/python -m pytest services/services/dispatcher/tests/test_forwarder.py -v
.venv/bin/python -m pytest services/services/dispatcher/tests/ -v
```
Expected: 4xx 测试 RED→GREEN；全 dispatcher 测试不回归。

- [ ] **Step 5: ruff/mypy + commit**

```bash
ruff check services/services/dispatcher/src/dispatcher/forwarder.py services/services/dispatcher/tests/test_forwarder.py
mypy services/services/dispatcher/src/dispatcher/forwarder.py
git add services/services/dispatcher/src/dispatcher/forwarder.py services/services/dispatcher/tests/test_forwarder.py
git commit -m "fix(dispatcher): _forward_stream eager-open 透传上游 status_code（修 SSE 4xx/5xx→200）+ test_forwarder"
```

---

## 风险 / 注意

- **mock snap + endpoint**：测试绕 DB（monkeypatch resolver 返 streaming snap）+ 走 dispatcher 转发路径。参照 `test_resolver.py`/`test_visibility.py` 的 monkeypatch 模式。dispatch 路径变量按 routes.py 实际。
- **httpx stream mock**：`_FakeStreamCM.__aenter__` 模拟连接建立（成功返 resp / 失败抛 RequestError）；`aiter_bytes` 模拟迭代（成功 yield chunks / 失败抛）。覆盖 eager-open + mid-stream 两段。
- **`get_forwarder()` 单例**：dispatcher forwarder 全局单例，monkeypatch 其 `_client.stream`。注意测试间状态隔离。
- **emit 用真上游 status**（mid-stream 失败）：语义变化（vs 旧 503），`test_stream_midstream_fail` 断言 emit 记 status_code=200（mock `_emit_stream_complete` 或查 emit 调用）。
