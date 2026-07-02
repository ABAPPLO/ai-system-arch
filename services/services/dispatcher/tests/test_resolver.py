"""resolver path 匹配测试 —— 纯函数，不依赖 PG。"""

import pytest

from dispatcher.resolver import _match_path, _extract_path_params


class TestPathMatch:
    @pytest.mark.parametrize("pattern,actual,expected", [
        ("/v1/users/{user_id}", "/v1/users/u_001", True),
        ("/v1/users/{user_id}", "/v1/users/u_001/orders", False),
        ("/v1/users/{user_id}", "/v1/users/", False),       # 段数不等
        ("/v1/health",          "/v1/health", True),
        ("/v1/health",          "/v1/healthz", False),
        ("/v1/{org}/repos",     "/v1/acme/repos", True),
        ("/v1/{org}/repos",     "/v1/acme/repo", False),    # repos != repo
        ("/v1/orders/{order_id}/items/{item_id}",
         "/v1/orders/o1/items/i1", True),
        ("/v1/orders/{order_id}/items/{item_id}",
         "/v1/orders/o1/items", False),
    ])
    def test_match(self, pattern, actual, expected):
        assert _match_path(pattern, actual) is expected


class TestExtractPathParams:
    def test_single_var(self):
        params = _extract_path_params("/v1/users/{user_id}", "/v1/users/u_001")
        assert params == {"user_id": "u_001"}

    def test_multiple_vars(self):
        params = _extract_path_params(
            "/v1/orders/{order_id}/items/{item_id}",
            "/v1/orders/o_001/items/i_042",
        )
        assert params == {"order_id": "o_001", "item_id": "i_042"}

    def test_no_vars(self):
        params = _extract_path_params("/v1/health", "/v1/health")
        assert params == {}

    def test_var_captures_any_segment(self):
        # 段内可含字母数字 / dash
        params = _extract_path_params("/v1/users/{user_id}", "/v1/users/abc-123_XYZ")
        assert params == {"user_id": "abc-123_XYZ"}
