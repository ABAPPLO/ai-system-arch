"""openapi_gen 单测 —— 验证 ApiMeta → OpenAPI spec 的转换逻辑。"""

from docs.models import ApiMeta
from docs.openapi_gen import (
    _example_from_schema,
    _infer_method,
    build_openapi_spec,
)


def _basic_meta(**kwargs) -> ApiMeta:
    defaults = {
        "api_id": "api_abc",
        "api_name": "Echo API",
        "description": "回显接口",
        "category": "utility",
        "base_path": "/echo",
        "tags": ["test"],
        "api_status": "published",
        "version_id": "ver_xyz",
        "version": "v1",
        "backend_type": "http",
        "backend_url": "http://backend.internal/echo",
        "version_status": "published",
    }
    defaults.update(kwargs)
    return ApiMeta(**defaults)


class TestBuildSpec:
    def test_basic_structure(self):
        meta = _basic_meta()
        spec = build_openapi_spec(meta)
        assert spec["openapi"] == "3.0.3"
        assert spec["info"]["title"] == "Echo API"
        assert spec["info"]["version"] == "v1"
        assert "/echo" in spec["paths"]
        assert "get" in spec["paths"]["/echo"]

    def test_security_scheme(self):
        spec = build_openapi_spec(_basic_meta())
        assert "ApiKeyAuth" in spec["components"]["securitySchemes"]
        assert spec["components"]["securitySchemes"]["ApiKeyAuth"]["in"] == "header"

    def test_x_apihub_extension(self):
        spec = build_openapi_spec(_basic_meta())
        assert spec["x-apihub"]["api_id"] == "api_abc"
        assert spec["x-apihub"]["version"] == "v1"

    def test_responses_include_standard_errors(self):
        spec = build_openapi_spec(_basic_meta())
        responses = spec["paths"]["/echo"]["get"]["responses"]
        for code in ("200", "400", "401", "403", "404", "429", "500"):
            assert code in responses

    def test_request_body_present_when_schema_given(self):
        meta = _basic_meta(
            backend_type="async_task",
            request_schema={
                "type": "object",
                "properties": {"msg": {"type": "string"}},
            },
        )
        spec = build_openapi_spec(meta)
        op = spec["paths"]["/echo"]["post"]
        assert "requestBody" in op
        assert op["requestBody"]["required"] is True

    def test_no_request_body_for_get(self):
        spec = build_openapi_spec(_basic_meta())
        op = spec["paths"]["/echo"]["get"]
        assert "requestBody" not in op

    def test_response_schema_with_example(self):
        meta = _basic_meta(
            response_schema={
                "type": "object",
                "properties": {"echo": {"type": "string"}},
                "example": {"echo": "hi"},
            },
        )
        spec = build_openapi_spec(meta)
        body = spec["paths"]["/echo"]["get"]["responses"]["200"]["content"]["application/json"]
        assert body["example"] == {"echo": "hi"}


class TestInferMethod:
    def test_http_get(self):
        assert _infer_method(_basic_meta(backend_type="http")) == "get"

    def test_async_post(self):
        assert _infer_method(_basic_meta(backend_type="async_task")) == "post"

    def test_workflow_post(self):
        assert _infer_method(_basic_meta(backend_type="workflow")) == "post"

    def test_ai_model_post(self):
        assert _infer_method(_basic_meta(backend_type="ai_model")) == "post"


class TestExampleFromSchema:
    def test_object_with_properties(self):
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "age": {"type": "integer"},
            },
        }
        ex = _example_from_schema(schema)
        assert ex == {"name": "", "age": 0}

    def test_array(self):
        schema = {"type": "array", "items": {"type": "string"}}
        assert _example_from_schema(schema) == [""]

    def test_explicit_example_wins(self):
        schema = {"type": "string", "example": "hello"}
        assert _example_from_schema(schema) == "hello"

    def test_default_used_when_no_example(self):
        schema = {"type": "string", "default": "fallback"}
        assert _example_from_schema(schema) == "fallback"

    def test_no_type_returns_none(self):
        assert _example_from_schema({}) is None
