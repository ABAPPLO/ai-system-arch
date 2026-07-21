"""AESGCM secret 加解密单测。"""

import pytest


@pytest.fixture(autouse=True)
def _set_key(monkeypatch):
    monkeypatch.setenv("HMAC_SECRET_KEY", "a" * 64)  # 32-byte hex
    from apihub_core.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_round_trip():
    from apihub_core.crypto import decrypt_secret, encrypt_secret
    ct = encrypt_secret("ak_supersecret_value")
    assert decrypt_secret(ct) == "ak_supersecret_value"


def test_ciphertext_nondeterministic():
    from apihub_core.crypto import encrypt_secret
    a = encrypt_secret("same_secret")
    b = encrypt_secret("same_secret")
    assert a != b  # AESGCM nonce 随机


def test_missing_key_raises(monkeypatch):
    monkeypatch.setenv("HMAC_SECRET_KEY", "")
    from apihub_core.config import get_settings
    get_settings.cache_clear()
    from apihub_core.crypto import encrypt_secret
    with pytest.raises(RuntimeError, match="HMAC_SECRET_KEY not configured"):
        encrypt_secret("x")


def test_tampered_ciphertext_raises():
    import base64

    from apihub_core.crypto import decrypt_secret, encrypt_secret
    from cryptography.exceptions import InvalidTag
    ct = encrypt_secret("secret")
    raw = bytearray(base64.b64decode(ct))
    raw[-1] ^= 0xFF  # flip a byte in tag
    tampered = base64.b64encode(bytes(raw)).decode()
    with pytest.raises(InvalidTag):
        decrypt_secret(tampered)
