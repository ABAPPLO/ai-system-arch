import pytest
from ai_gateway.crypto import encrypt
from ai_gateway.models import RouteResult, SSEChunk


@pytest.mark.asyncio
async def test_model_not_found(client, monkeypatch):
    async def mock_resolve(model): return None
    monkeypatch.setattr("ai_gateway.routes.resolve_model_route", mock_resolve)
    resp = await client.post("/v1/chat/completions", json={"model":"unknown","messages":[{"role":"user","content":"Hi"}]})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_chat_completions_ok(client, monkeypatch):
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
