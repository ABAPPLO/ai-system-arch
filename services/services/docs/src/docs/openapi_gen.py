"""OpenAPI 3.0 spec 生成 —— 基于 api + api_version 元数据。

请求 / 响应 schema 由接口发布者在 api-registry 配置（JSON Schema 风格），
这里转成 OpenAPI 的 components/schemas + operation.requestBody.responses。
"""

from typing import Any

from docs.models import ApiMeta


def build_openapi_spec(
    meta: ApiMeta, *, base_url: str = "https://api.apihub.example"
) -> dict[str, Any]:
    """生成 OpenAPI 3.0.3 文档。"""
    method = _infer_method(meta)
    path = meta.base_path
    summary = meta.api_name
    description = meta.description or ""

    operation: dict[str, Any] = {
        "summary": summary,
        "description": description,
        "operationId": f"{method}_{meta.api_id}_{meta.version}",
        "tags": [meta.category] + (meta.tags if meta.tags else []),
        "security": [{"ApiKeyAuth": []}],
    }

    parameters = _build_parameters(meta)
    if parameters:
        operation["parameters"] = parameters

    if meta.request_schema:
        operation["requestBody"] = {
            "required": True,
            "content": {
                "application/json": {
                    "schema": _jsonschema_to_openapi(meta.request_schema),
                    "example": _example_from_schema(meta.request_schema),
                }
            },
        }

    operation["responses"] = _build_responses(meta)

    spec: dict[str, Any] = {
        "openapi": "3.0.3",
        "info": {
            "title": meta.api_name,
            "version": meta.version,
            "description": description,
        },
        "servers": [{"url": f"{base_url.rstrip('/')}/v1"}],
        "paths": {
            path: {
                method: operation,
            }
        },
        "components": {
            "securitySchemes": {
                "ApiKeyAuth": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-API-Key",
                    "description": "在 apihub 控制台生成的 API Key",
                }
            }
        },
        "x-apihub": {
            "api_id": meta.api_id,
            "version_id": meta.version_id,
            "version": meta.version,
            "backend_type": meta.backend_type,
            "api_status": meta.api_status,
            "version_status": meta.version_status,
            "ai_model": meta.ai_model,
            "ai_streaming": meta.ai_streaming,
        },
    }
    return spec


def _infer_method(meta: ApiMeta) -> str:
    """根据 backend_type 推断默认方法。

    AI 流式 → POST（SSE）；async_task → POST；其他 → GET（可被 schema 覆盖）。
    Phase 1 简化：固定 GET for HTTP，POST for async/AI。
    """
    if meta.backend_type in ("async_task", "workflow", "ai_model"):
        return "post"
    return "get"


def _build_parameters(meta: ApiMeta) -> list[dict[str, Any]]:
    """从 request_schema 的 query 子节点抽 OpenAPI parameters。

    Phase 1 简化：request_schema 假设是 body schema；query 参数走 base_path
    上的 {param} 占位符 —— 暂不展开。预留扩展点。
    """
    return []


def _build_responses(meta: ApiMeta) -> dict[str, Any]:
    responses: dict[str, Any] = {}
    if meta.response_schema:
        responses["200"] = {
            "description": "成功响应",
            "content": {
                "application/json": {
                    "schema": _jsonschema_to_openapi(meta.response_schema),
                    "example": _example_from_schema(meta.response_schema),
                }
            },
        }
    else:
        responses["200"] = {"description": "成功响应"}

    # 标准错误（统一错误模型 docs/02）
    for code, msg in (
        (400, "请求参数错误"),
        (401, "未认证 / API Key 无效"),
        (403, "无权限"),
        (404, "接口或资源不存在"),
        (429, "触发限流"),
        (500, "服务端错误"),
    ):
        responses[str(code)] = {
            "description": msg,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "success": {"type": "boolean", "example": False},
                            "code": {"type": "integer", "example": code},
                            "message": {"type": "string"},
                            "trace_id": {"type": "string"},
                        },
                    }
                }
            },
        }
    return responses


def _jsonschema_to_openapi(schema: dict[str, Any]) -> dict[str, Any]:
    """JSON Schema → OpenAPI Schema。

    OpenAPI 3.0 的 schema 子集 ≈ JSON Schema draft-07，直接转多数情况下够用。
    复杂场景（$ref 跨文件等）Phase 2 在线渲染时再扩展。
    """
    if not isinstance(schema, dict):
        return {"type": "object"}
    out = dict(schema)
    return out


def _example_from_schema(schema: dict[str, Any]) -> Any:
    """根据 schema 生成默认 example —— 让 OpenAPI preview 有东西展示。"""
    if not isinstance(schema, dict):
        return None
    if "example" in schema:
        return schema["example"]
    if "default" in schema:
        return schema["default"]
    t = schema.get("type")
    if t == "object":
        props = schema.get("properties") or {}
        return {k: _example_from_schema(v) for k, v in props.items()}
    if t == "array":
        items = schema.get("items") or {}
        return [_example_from_schema(items)]
    if t == "string":
        return ""
    if t in ("integer", "number"):
        return 0
    if t == "boolean":
        return False
    return None
