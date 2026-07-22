"""PII 加解密测试。"""

import os

# Settings() 需要这些 env 才能初始化
os.environ.setdefault("PG_HOST", "localhost")
os.environ.setdefault("PG_USER", "apihub")
os.environ.setdefault("PG_PASSWORD", "test")
os.environ.setdefault("PG_DATABASE", "apihub")
os.environ.setdefault("REDIS_HOST", "localhost")

from apihub_core.pii import decrypt_pii, encrypt_pii, maybe_decrypt  # noqa: E402


class TestPiiEncryptDecrypt:
    def test_roundtrip(self):
        plain = "13800138000"
        encrypted = encrypt_pii(plain)
        assert encrypted != plain
        assert decrypt_pii(encrypted) == plain

    def test_different_ivs(self):
        plain = "hello@example.com"
        e1 = encrypt_pii(plain)
        e2 = encrypt_pii(plain)
        assert e1 != e2
        assert decrypt_pii(e1) == plain
        assert decrypt_pii(e2) == plain

    def test_decrypt_wrong_key_raises(self, monkeypatch):
        encrypted = encrypt_pii("test")
        fake = type("s", (), {"pii_encryption_key": "0000000000000000000000000000000000000000000000000000000000000000"})
        monkeypatch.setattr("apihub_core.pii.get_settings", lambda: fake())
        import pytest
        with pytest.raises(Exception):  # noqa: B017  解密失败异常类型依后端实现，保持宽匹配避免漏捕
            decrypt_pii(encrypted)

    def test_empty_string(self):
        assert decrypt_pii(encrypt_pii("")) == ""


class TestMaybeDecrypt:
    def test_unencrypted_passthrough(self):
        assert maybe_decrypt("plain-text") == "plain-text"

    def test_none_returns_empty(self):
        assert maybe_decrypt(None) == ""

    def test_empty_returns_empty(self):
        assert maybe_decrypt("") == ""

    def test_encrypted_value_decrypted(self):
        plain = "Alice"
        encrypted = encrypt_pii(plain)
        assert maybe_decrypt(encrypted) == plain
