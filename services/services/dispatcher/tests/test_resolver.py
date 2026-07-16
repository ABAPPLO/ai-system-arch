"""resolver path 匹配测试 —— 纯函数，不依赖 PG。

注：resolve_by_path / _match_path 已于 R1c §3 移除（dispatcher 退纯转发，
强制 X-API-Version-Id）。保留 _extract_path_params 的单测。
"""

from dispatcher.resolver import _extract_path_params


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
