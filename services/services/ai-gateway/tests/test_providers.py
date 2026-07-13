import pytest
from ai_gateway.providers.anthropic import AnthropicProvider
from ai_gateway.providers.openai_compat import OpenAICompatibleProvider


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
        def mock_stream(self, method, url, **kw):
            class FakeResp:
                async def __aenter__(self): return self
                async def __aexit__(self, *a): pass
                async def raise_for_status(self): pass
                async def aiter_lines(self):
                    for line in ['data: {"choices":[{"delta":{"content":"Hello"},"index":0}]}', 'data: {"choices":[{"delta":{},"index":0,"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":20,"total_tokens":30}}', 'data: [DONE]']:
                        yield line
            return FakeResp()
        monkeypatch.setattr("httpx.AsyncClient.stream", mock_stream)
        provider = OpenAICompatibleProvider()
        chunks = [c async for c in provider.chat_completion(messages=[{"role":"user","content":"Hi"}], model="gpt-4o-mini", api_key="sk-test", base_url="https://test.api.com/v1", stream=True)]
        assert chunks[0].content == "Hello"
        assert chunks[-2].usage["total_tokens"] == 30


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
        def mock_stream(self, method, url, **kw):
            class FakeResp:
                async def __aenter__(self): return self
                async def __aexit__(self, *a): pass
                async def raise_for_status(self): pass
                async def aiter_lines(self):
                    for line in ['event: content_block_start', 'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
                              'event: content_block_delta', 'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello"}}',
                              'event: content_block_delta', 'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":" world"}}',
                              'event: message_delta', 'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"input_tokens":15,"output_tokens":10}}',
                              'event: message_stop', 'data: {"type":"message_stop"}']:
                        yield line
            return FakeResp()
        monkeypatch.setattr("httpx.AsyncClient.stream", mock_stream)
        provider = AnthropicProvider()
        chunks = [c async for c in provider.chat_completion(messages=[{"role":"user","content":"Hi"}], model="claude-sonnet-4", api_key="sk-ant-test", base_url="https://api.anthropic.com", stream=True)]
        assert any("Hello" in c.content for c in chunks)
        assert any("world" in c.content for c in chunks)
        usage_chunks = [c for c in chunks if c.usage]
        assert len(usage_chunks) >= 1
