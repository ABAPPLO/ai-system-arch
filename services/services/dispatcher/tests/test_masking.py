"""masking 单测 —— 各种 action / 嵌套字段 / 数组。"""

import hashlib

import pytest
from dispatcher.masking import _smart_mask, apply_masking, mask_value


class TestMaskValue:
    def test_remove(self):
        assert mask_value("secret", "remove") is None

    def test_hash(self):
        h = mask_value("foo", "hash")
        assert h == hashlib.sha256(b"foo").hexdigest()[:16]

    def test_mask_phone(self):
        assert mask_value("13812341234", "mask") == "138****1234"

    def test_mask_email(self):
        assert mask_value("alice@example.com", "mask") == "a***@example.com"

    def test_mask_id_card(self):
        assert mask_value("110101199001011234", "mask") == "110101********1234"

    def test_mask_short_string(self):
        assert mask_value("ab", "mask") == "**"

    def test_mask_unknown_action_returns_original(self):
        assert mask_value("x", "unknown") == "x"


class TestSmartMask:
    @pytest.mark.parametrize("inp,expected", [
        ("13800000001",   "138****0001"),
        ("alice@x.com",   "a***@x.com"),
        ("bob@test.org",  "b***@test.org"),
        ("110101199001011234", "110101********1234"),
        ("abc",           "***"),            # len<=4 全 *
        ("a",             "*"),
        ("+8613800000001", "+************1"), # 14 字符，默认首尾保留 + 中间 12 星
    ])
    def test_various(self, inp, expected):
        assert _smart_mask(inp) == expected


class TestApplyMasking:
    def test_no_rules_returns_original(self):
        data = {"a": 1}
        assert apply_masking(data, None) == data
        assert apply_masking(data, []) == data

    def test_top_level_remove(self):
        data = {"name": "Alice", "password": "x"}
        result = apply_masking(data, [{"field": "password", "action": "remove"}])
        assert result == {"name": "Alice"}

    def test_top_level_mask(self):
        data = {"name": "Alice", "phone": "13800001234"}
        result = apply_masking(data, [{"field": "phone", "action": "mask"}])
        assert result["phone"] == "138****1234"
        # 原数据不动
        assert data["phone"] == "13800001234"

    def test_nested_field(self):
        data = {"user": {"name": "Alice", "phone": "13800001234"}}
        result = apply_masking(data, [{"field": "user.phone", "action": "mask"}])
        assert result["user"]["phone"] == "138****1234"
        assert result["user"]["name"] == "Alice"

    def test_array_field(self):
        data = {"users": [
            {"id": 1, "phone": "13800001234"},
            {"id": 2, "phone": "13900005678"},
        ]}
        result = apply_masking(data, [{"field": "users[].phone", "action": "mask"}])
        assert result["users"][0]["phone"] == "138****1234"
        assert result["users"][1]["phone"] == "139****5678"
        # 其他字段不变
        assert result["users"][0]["id"] == 1

    def test_multiple_rules(self):
        data = {
            "name": "Alice",
            "phone": "13800001234",
            "id_card": "110101199001011234",
            "token": "sk_abc",
        }
        result = apply_masking(data, [
            {"field": "phone", "action": "mask"},
            {"field": "id_card", "action": "hash"},
            {"field": "token", "action": "remove"},
        ])
        assert result["phone"] == "138****1234"
        assert result["id_card"] == hashlib.sha256(b"110101199001011234").hexdigest()[:16]
        assert "token" not in result
        assert result["name"] == "Alice"

    def test_missing_field_no_error(self):
        data = {"a": 1}
        # 字段不存在不应抛异常
        result = apply_masking(data, [{"field": "b", "action": "mask"}])
        assert result == {"a": 1}

    def test_deeply_nested(self):
        data = {"a": {"b": {"c": {"d": "13800001234"}}}}
        result = apply_masking(data, [{"field": "a.b.c.d", "action": "mask"}])
        assert result["a"]["b"]["c"]["d"] == "138****1234"

    def test_empty_value(self):
        assert apply_masking(None, [{"field": "x", "action": "mask"}]) is None
        assert apply_masking({}, [{"field": "x", "action": "mask"}]) == {}
