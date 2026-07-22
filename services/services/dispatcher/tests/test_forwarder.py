"""dispatcher HttpForwarder 单测 —— SSE 透传上游 status_code + 同步转发。

回归核心：原 _forward_stream 在 generator 外构造 StreamingResponse（默认 200），
上游 status_code 仅在 generator 内 lazy 捕获 → 永远到不了 response，
SSE 路径 4xx/5xx 被吞成 HTTP 200。fix = eager-open：await cm.__aenter__()
先取 upstream.status_code 再构造 StreamingResponse(status_code=...)。

TDD：本文件先写（红）→ 修 forwarder.py（绿）。
"""

from dataclasses import asdict

import httpx
import pytest

# ---- httpx fakes（mock _client.stream / _client.request）----


class _FakeResp:
    """模拟 httpx streaming Response：status_code + aiter_bytes()。"""

    def __init__(self, status_code, chunks=None, stream_error=None):
        self.status_code = status_code
        self._chunks = chunks or []
        self._stream_error = stream_error

    async def aiter_bytes(self):
        for c in self._chunks:
            yield c
        if self._stream_error is not None:
            raise self._stream_error


class _FakeStreamCM:
    """模拟 `async with client.stream(...) as resp`。

    持 _FakeResp 或 Exception：__aenter__ 成功返 resp / 失败抛指定异常，
    覆盖 eager-open 的两段语义（连接建立 vs 迭代）。
    """

    def __init__(self, resp_or_exc):
        self._v = resp_or_exc

    async def __aenter__(self):
        if isinstance(self._v, Exception):
            raise self._v
        return self._v

    async def __aexit__(self, *a):
        return False


class _FakeSyncResp:
    """模拟 httpx 同步 Response：status_code + content + headers。"""

    def __init__(self, status_code=200, content=b'{"ok": true}', headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers if headers is not None else {
            "content-type": "application/json"
        }


# ---- fixtures ----


@pytest.fixture
def stubbed_forwarder(monkeypatch):
    """注入一个 HttpForwarder 到 dispatcher.routes._forwarder，client.stream /
    client.request 由测试 monkeypatch。monkeypatch 自动还原 _forwarder。"""
    from dispatcher.forwarder import HttpForwarder

    # 用真实 httpx.AsyncClient 作底座（不会真发请求 —— stream/request 被 monkeypatch）。
    # trust_env=False：避免测试机 socks proxy 环境变量让构造期就抛。
    fwd = HttpForwarder(httpx.AsyncClient(trust_env=False))
    monkeypatch.setattr("dispatcher.routes._forwarder", fwd)
    return fwd


@pytest.fixture
def stub_kafka(monkeypatch):
    """吞掉 apihub_core.kafka.emit_event；记录所有调用。返回 events 列表。"""
    from apihub_core import kafka

    events: list = []

    async def _swallow(event):
        events.append(event)

    monkeypatch.setattr(kafka, "emit_event", _swallow)
    return events


def _streaming_snap():
    from dispatcher.models import ApiVersionSnapshot

    return ApiVersionSnapshot(
        id="ver_stream",
        api_id="api_llm",
        tenant_id="tenant_a",
        version="v1",
        backend_type="ai_model",
        backend_url="http://up.test/llm",
        method="POST",
        path="/llm",
        masking=None,
        rate_limit=None,
        retry_policy=None,
        cache_policy=None,
        ai_model="gpt-test",
        ai_streaming=True,
        ai_params=None,
        sla_p99_ms=None,
        sla_availability=None,
        timeout_ms=30_000,
        visibility="public",
    )


def _sync_snap():
    from dispatcher.models import ApiVersionSnapshot

    return ApiVersionSnapshot(
        id="ver_sync",
        api_id="api_http",
        tenant_id="tenant_a",
        version="v1",
        backend_type="http",
        backend_url="http://up.test/sync",
        method="POST",
        path="/sync",
        masking=None,
        rate_limit=None,
        retry_policy=None,
        cache_policy=None,
        ai_model=None,
        ai_streaming=False,
        ai_params=None,
        sla_p99_ms=None,
        sla_availability=None,
        timeout_ms=30_000,
        visibility="public",
    )


def _patch_resolve_to(monkeypatch, snap):
    """绕 DB：dispatcher.routes.resolve_by_header → 返 snap。"""
    from dispatcher import routes

    async def _resolve(version_id):
        return snap

    monkeypatch.setattr(routes, "resolve_by_header", _resolve)


_DISPATCH_HEADERS = {"X-API-Version-Id": "ver_stream", "X-API-Key": "ak_test_a_demo001"}


# ===================== SSE streaming tests =====================


async def test_stream_upstream_4xx_propagated(
    async_client, monkeypatch, stubbed_forwarder, stub_kafka
):
    """核心回归：上游 SSE 404 → 客户端收 404（旧 bug：收 200）。"""
    _patch_resolve_to(monkeypatch, _streaming_snap())

    def _fake_stream(*a, **kw):
        return _FakeStreamCM(
            _FakeResp(404, chunks=[b'data: {"error":"not found"}\n\n'])
        )

    monkeypatch.setattr(stubbed_forwarder._client, "stream", _fake_stream)

    resp = await async_client.post(
        "/dispatch/llm", headers=_DISPATCH_HEADERS, json={"prompt": "hi"}
    )
    assert resp.status_code == 404, f"SSE 上游 4xx 应透传，got {resp.status_code}"
    assert b"not found" in resp.content


async def test_stream_normal_200(
    async_client, monkeypatch, stubbed_forwarder, stub_kafka
):
    """上游 SSE 200 + usage chunk → 200 + body 透传 + emit_stream_complete 记 token。"""
    _patch_resolve_to(monkeypatch, _streaming_snap())
    chunks = [
        b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
        b'data: {"usage":{"prompt_tokens":12,"completion_tokens":8,"total_tokens":20},"choices":[]}\n\n',
        b"data: [DONE]\n\n",
    ]

    def _fake_stream(*a, **kw):
        return _FakeStreamCM(_FakeResp(200, chunks=chunks))

    monkeypatch.setattr(stubbed_forwarder._client, "stream", _fake_stream)

    resp = await async_client.post(
        "/dispatch/llm", headers=_DISPATCH_HEADERS, json={"prompt": "hi"}
    )
    assert resp.status_code == 200, resp.text
    assert b"hi" in resp.content
    assert b"[DONE]" in resp.content
    # emit_stream_complete 被调一次，记 200 + token 用量
    assert len(stub_kafka) == 1
    payload = asdict(stub_kafka[0])
    assert payload["status_code"] == 200
    assert payload["token_prompt"] == 12
    assert payload["token_completion"] == 8
    assert payload["ai_streaming"] == 1


async def test_stream_5xx_propagated(
    async_client, monkeypatch, stubbed_forwarder, stub_kafka
):
    """上游 SSE 500 → 客户端收 500（status_code 透传）。"""
    _patch_resolve_to(monkeypatch, _streaming_snap())

    def _fake_stream(*a, **kw):
        return _FakeStreamCM(_FakeResp(500, chunks=[b'data: {"error":"boom"}\n\n']))

    monkeypatch.setattr(stubbed_forwarder._client, "stream", _fake_stream)

    resp = await async_client.post(
        "/dispatch/llm", headers=_DISPATCH_HEADERS, json={"prompt": "hi"}
    )
    assert resp.status_code == 500, resp.text
    assert b"boom" in resp.content


async def test_stream_conn_fail_503(
    async_client, monkeypatch, stubbed_forwarder, stub_kafka
):
    """上游连接建立失败（__aenter__ 抛 ConnectError）→ 503 + _emit_failure 记 API_DOWN。"""
    _patch_resolve_to(monkeypatch, _streaming_snap())

    def _fake_stream(*a, **kw):
        return _FakeStreamCM(httpx.ConnectError("conn refused"))

    monkeypatch.setattr(stubbed_forwarder._client, "stream", _fake_stream)

    resp = await async_client.post(
        "/dispatch/llm", headers=_DISPATCH_HEADERS, json={"prompt": "hi"}
    )
    assert resp.status_code == 503, resp.text
    # _emit_failure 被调一次
    assert len(stub_kafka) == 1
    payload = asdict(stub_kafka[0])
    assert payload["status_code"] == 503
    assert payload["error_code"] == "API_DOWN"


async def test_stream_midstream_fail_uses_upstream_status(
    async_client, monkeypatch, stubbed_forwarder, stub_kafka
):
    """上游 200 先发 chunk，迭代中抛 ReadError → emit 用真上游 status=200（旧 bug 记 503）。

    语义变化：旧实现 mid-stream fail 时强制 status_code=503；新实现保留 eager-open
    捕获的真上游 status（=200），更如实反映「上游已返 header、连接中断」。
    """
    _patch_resolve_to(monkeypatch, _streaming_snap())
    chunks = [b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n']

    def _fake_stream(*a, **kw):
        return _FakeStreamCM(
            _FakeResp(200, chunks=chunks, stream_error=httpx.ReadError("rst"))
        )

    monkeypatch.setattr(stubbed_forwarder._client, "stream", _fake_stream)

    resp = await async_client.post(
        "/dispatch/llm", headers=_DISPATCH_HEADERS, json={"prompt": "hi"}
    )
    # 客户端看到 200（status 在迭代前已定）+ body 含原 chunk 和 error event
    assert resp.status_code == 200, resp.text
    assert b"hi" in resp.content
    assert b'{"error":"backend error"}' in resp.content
    # emit_stream_complete 用 status_code=200（旧 bug 会记 503）
    assert len(stub_kafka) == 1
    payload = asdict(stub_kafka[0])
    assert payload["status_code"] == 200


# ===================== _forward_sync tests（顺带覆盖）=====================


_SYNC_HEADERS = {"X-API-Version-Id": "ver_sync", "X-API-Key": "ak_test_a_demo001"}


async def test_sync_normal(
    async_client, monkeypatch, stubbed_forwarder, stub_kafka
):
    """_forward_sync happy path：上游 200 JSON → 透传 body + _emit_success 记 200。"""
    _patch_resolve_to(monkeypatch, _sync_snap())

    async def _fake_request(*a, **kw):
        return _FakeSyncResp(200, content=b'{"ok": true}')

    monkeypatch.setattr(stubbed_forwarder._client, "request", _fake_request)

    resp = await async_client.post(
        "/dispatch/sync", headers=_SYNC_HEADERS, json={"q": "hi"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True}
    assert len(stub_kafka) == 1
    payload = asdict(stub_kafka[0])
    assert payload["status_code"] == 200


async def test_sync_error_503(
    async_client, monkeypatch, stubbed_forwarder, stub_kafka
):
    """_forward_sync 上游连接失败 → 503 + _emit_failure 记 API_DOWN。"""
    _patch_resolve_to(monkeypatch, _sync_snap())

    async def _fake_request(*a, **kw):
        raise httpx.ConnectError("conn refused")

    monkeypatch.setattr(stubbed_forwarder._client, "request", _fake_request)

    resp = await async_client.post(
        "/dispatch/sync", headers=_SYNC_HEADERS, json={"q": "hi"}
    )
    assert resp.status_code == 503, resp.text
    assert len(stub_kafka) == 1
    payload = asdict(stub_kafka[0])
    assert payload["status_code"] == 503
    assert payload["error_code"] == "API_DOWN"


def test_build_forward_headers_strips_hmac_and_bearer_credentials():
    """I4（review）：dispatcher 不把 HMAC 签名凭证透传给上游。

    否则上游 middleware 见 X-App-Key 会跑 _verify_hmac，而上游服务未必注入了
    HMAC_SECRET_KEY → 500；且签名是给 dispatcher 入口验的，非后端。
    """
    from dispatcher.forwarder import _build_forward_headers

    class _Req:
        def __init__(self, h):
            self.headers = h

    req = _Req({
        "X-App-Key": "ak_x", "X-Signature": "deadbeef",
        "X-Timestamp": "1", "X-Nonce": "n",
        "X-API-Key": "ak_bearer", "Authorization": "Bearer t",
        "Content-Type": "application/json", "X-Request-Id": "r1",
    })
    out = _build_forward_headers(req)
    out_lower = {k.lower() for k in out}
    # 调用方凭证（bearer + HMAC 签名流）全剥
    for h in ("x-app-key", "x-signature", "x-timestamp", "x-nonce", "x-api-key", "authorization"):
        assert h not in out_lower, f"{h} 不该透传给上游"
    # 非凭证头保留
    assert "x-request-id" in out_lower
