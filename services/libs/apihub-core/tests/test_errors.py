"""Errors 测试 —— 错误码到 HTTP 状态映射 + ApiError 构造。"""

import pytest
from apihub_core.errors import (
    _HTTP_STATUS_MAP,
    ApiError,
    ErrorCode,
    api_error_handler,
    unhandled_exception_handler,
)
from fastapi import Request


class TestErrorCodeMapping:
    def test_all_codes_have_http_status(self):
        """所有 ErrorCode 必须有明确 HTTP 映射或落到 500 默认。"""
        for code in ErrorCode:
            status = _HTTP_STATUS_MAP.get(code, 500)
            assert 200 <= status < 600, f"{code.name}: {status} out of range"

    @pytest.mark.parametrize(
        "code,expected",
        [
            (ErrorCode.INVALID_PARAMS, 400),
            (ErrorCode.UNAUTHORIZED, 401),
            (ErrorCode.FORBIDDEN, 403),
            (ErrorCode.NOT_FOUND, 404),
            (ErrorCode.RATE_LIMITED, 429),
            (ErrorCode.TENANT_QUOTA_EXCEEDED, 429),
            (ErrorCode.TENANT_DISABLED, 403),
            (ErrorCode.API_DEPRECATED, 410),
            (ErrorCode.API_DOWN, 503),
            (ErrorCode.TASK_TIMEOUT, 504),
            (ErrorCode.INTERNAL, 500),
        ],
    )
    def test_specific_mappings(self, code, expected):
        assert _HTTP_STATUS_MAP[code] == expected

    def test_unique_codes(self):
        """错误码数值唯一。"""
        values = [c.value for c in ErrorCode]
        assert len(values) == len(set(values))


class TestApiError:
    def test_basic_construction(self):
        err = ApiError(ErrorCode.NOT_FOUND, "not found")
        assert err.code == ErrorCode.NOT_FOUND
        assert err.message == "not found"
        assert err.http_status == 404
        assert err.details == {}

    def test_explicit_http_status_override(self):
        err = ApiError(ErrorCode.INVALID_PARAMS, "bad", http_status=422)
        assert err.http_status == 422

    def test_details_dict(self):
        err = ApiError(
            ErrorCode.INVALID_PARAMS,
            "bad",
            details={"field": "user_id", "reason": "missing"},
        )
        assert err.details == {"field": "user_id", "reason": "missing"}

    def test_str_representation_includes_code(self):
        err = ApiError(ErrorCode.NOT_FOUND, "missing")
        assert "NOT_FOUND" in str(err)
        assert "missing" in str(err)


def _make_request(path: str = "/v1/test") -> Request:
    """构造一个最小可用 Request（fastapi.Request 只读少量字段）。"""
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "headers": [],
        "query_string": b"",
        "scheme": "http",
        "server": ("test", 80),
        "client": ("127.0.0.1", 12345),
    }
    return Request(scope)


class TestErrorHandlers:
    def test_api_error_handler_returns_correct_json(self):
        err = ApiError(ErrorCode.NOT_FOUND, "API xxx not found", details={"api_id": "xxx"})
        request = _make_request()

        resp = api_error_handler(request, err)

        assert resp.status_code == 404
        body = getattr(resp, "body", b"{}")
        import json

        data = json.loads(body)
        assert data["success"] is False
        assert data["code"] == ErrorCode.NOT_FOUND.value
        assert data["message"] == "API xxx not found"
        assert data["details"]["api_id"] == "xxx"

    def test_api_error_handler_preserves_http_status(self):
        """http_status 不在标准映射里时也应保留。"""
        err = ApiError(ErrorCode.INVALID_PARAMS, "bad", http_status=422)
        request = _make_request()
        resp = api_error_handler(request, err)
        assert resp.status_code == 422

    def test_unhandled_exception_returns_500(self):
        request = _make_request()
        resp = unhandled_exception_handler(request, RuntimeError("boom"))
        assert resp.status_code == 500
        import json

        body = getattr(resp, "body", b"{}")
        data = json.loads(body)
        assert data["code"] == ErrorCode.INTERNAL.value
        # 不要泄漏内部错误细节
        assert "boom" not in data.get("message", "")
