"""APIKey 生成 / 哈希 / 格式校验 纯函数测试。"""

import hashlib
import re

import pytest
from auth.apikey import (
    KEY_PREFIX,
    cache_key,
    generate_api_key,
    hash_api_key,
    is_valid_format,
)


class TestGenerateApiKey:
    def test_returns_three_values(self):
        result = generate_api_key()
        assert len(result) == 3

    def test_plaintext_has_correct_prefix(self):
        plaintext, _, _ = generate_api_key()
        assert plaintext.startswith(KEY_PREFIX)

    def test_plaintext_long_enough(self):
        plaintext, _, _ = generate_api_key()
        # ak_ + 至少 32 字符随机
        assert len(plaintext) > 32

    def test_hash_is_sha256_hex(self):
        plaintext, key_hash, _ = generate_api_key()
        assert key_hash == hashlib.sha256(plaintext.encode()).hexdigest()
        assert len(key_hash) == 64
        assert re.fullmatch(r"[0-9a-f]{64}", key_hash)

    def test_display_prefix_is_first_8_chars(self):
        plaintext, _, display = generate_api_key()
        assert display == plaintext[:8]
        assert display.startswith(KEY_PREFIX)

    def test_each_call_unique(self):
        keys = {generate_api_key()[0] for _ in range(50)}
        assert len(keys) == 50

    def test_url_safe_chars_only(self):
        """token_urlsafe 不含 / + 等会破坏 URL/header 的字符。"""
        for _ in range(20):
            plaintext, _, _ = generate_api_key()
            # 允许 ak_ 前缀 + URL-safe base64 字符
            body = plaintext[len(KEY_PREFIX) :]
            assert re.fullmatch(r"[A-Za-z0-9_-]+", body), f"non-url-safe char in {plaintext!r}"


class TestHashApiKey:
    def test_deterministic(self):
        assert hash_api_key("ak_test123") == hash_api_key("ak_test123")

    def test_different_inputs_different_hash(self):
        assert hash_api_key("ak_a") != hash_api_key("ak_b")

    def test_handles_unicode(self):
        # 不应该出现在 APIKey 里，但函数不应崩
        h = hash_api_key("ak_测试")
        assert len(h) == 64


class TestCacheKey:
    def test_from_plaintext(self):
        plaintext = "ak_abc123"
        key = cache_key(plaintext)
        expected = f"ak:{hash_api_key(plaintext)}"
        assert key == expected

    def test_from_hash(self):
        """如果直接传 hash，不再 hash 一次。"""
        h = hash_api_key("ak_abc")
        key = cache_key(h)
        assert key == f"ak:{h}"

    def test_starts_with_ak_prefix(self):
        plaintext, _, _ = generate_api_key()
        assert cache_key(plaintext).startswith("ak:")


class TestIsValidFormat:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("ak_" + "a" * 30, True),
            ("ak_short", False),  # 太短
            ("ak_ab", False),  # 太短
            ("sk_other123456789012345678901234", False),  # 错前缀
            ("", False),
            (None, False),
            ("Bearer ak_xxx", False),  # 不要把 Authorization header 直接传进来
            ("ak_" + "x" * 50, True),  # 长 key 也 OK
        ],
    )
    def test_format(self, value, expected):
        assert is_valid_format(value) is expected

    def test_rejects_obvious_garbage(self):
        """防扫描：明显垃圾的请求快速拒绝，不打 DB。"""
        for garbage in ["password", "123456", "'; DROP TABLE--", "../../etc/passwd"]:
            assert is_valid_format(garbage) is False
