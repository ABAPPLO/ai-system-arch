"""examples 单测 —— 验证 curl/Python/JS 示例的生成。"""

from docs.examples import build_examples
from docs.models import ApiMeta


def _basic_meta(**kwargs) -> ApiMeta:
    defaults = {
        "api_id": "api_abc",
        "api_name": "Echo API",
        "description": "回显接口",
        "category": "utility",
        "base_path": "/echo",
        "tags": [],
        "api_status": "published",
        "version_id": "ver_xyz",
        "version": "v1",
        "backend_type": "http",
        "backend_url": "http://backend.internal/echo",
        "version_status": "published",
    }
    defaults.update(kwargs)
    return ApiMeta(**defaults)


class TestCurlExample:
    def test_get_basic(self):
        ex = build_examples(_basic_meta())
        assert "curl -X GET" in ex.curl
        assert "/echo" in ex.curl
        assert "X-API-Key" in ex.curl

    def test_post_includes_body(self):
        ex = build_examples(
            _basic_meta(backend_type="async_task", request_schema={"type": "object"})
        )
        assert "curl -X POST" in ex.curl
        assert "Content-Type" in ex.curl
        assert '-d' in ex.curl


class TestPythonExample:
    def test_get_uses_httpx(self):
        ex = build_examples(_basic_meta())
        assert "httpx" in ex.python
        assert "X-API-Key" in ex.python

    def test_streaming_ai(self):
        ex = build_examples(
            _basic_meta(
                backend_type="ai_model",
                ai_streaming=True,
                request_schema={"type": "object"},
            )
        )
        assert "httpx.stream" in ex.python
        assert "data: " in ex.python


class TestJavaScriptExample:
    def test_get_uses_fetch(self):
        ex = build_examples(_basic_meta())
        assert "fetch(" in ex.javascript
        assert "X-API-Key" in ex.javascript

    def test_streaming_ai(self):
        ex = build_examples(
            _basic_meta(
                backend_type="ai_model",
                ai_streaming=True,
                request_schema={"type": "object"},
            )
        )
        assert "getReader" in ex.javascript
        assert "data: " in ex.javascript


class TestNotes:
    def test_no_notes_for_clean_published(self):
        ex = build_examples(_basic_meta())
        assert ex.notes == []

    def test_masking_triggers_note(self):
        ex = build_examples(_basic_meta(masking={"fields": ["ssn"]}))
        assert any("脱敏" in n for n in ex.notes)

    def test_deprecated_triggers_note(self):
        ex = build_examples(_basic_meta(version_status="deprecated"))
        assert any("废弃" in n for n in ex.notes)

    def test_unpublished_api_status_triggers_note(self):
        ex = build_examples(_basic_meta(api_status="draft"))
        assert any("draft" in n for n in ex.notes)

    def test_streaming_note(self):
        ex = build_examples(
            _basic_meta(backend_type="ai_model", ai_streaming=True)
        )
        assert any("SSE" in n for n in ex.notes)
