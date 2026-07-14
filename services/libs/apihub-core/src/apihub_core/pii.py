"""PII 字段加解密 —— AES-256-GCM。

密钥来源：env PII_ENCRYPTION_KEY（64 hex chars = 32 bytes）。
密文格式：base64(nonce + ciphertext + tag)。
"""

import base64
import os

from apihub_core.config import get_settings

_NONCE_LENGTH = 12


def _get_key() -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: PLC0415

    key_hex = get_settings().pii_encryption_key
    if not key_hex:
        msg = "PII_ENCRYPTION_KEY not configured"
        raise RuntimeError(msg)
    return bytes.fromhex(key_hex)


def encrypt_pii(plaintext: str) -> str:
    """加密 PII 明文 → base64(nonce + ciphertext + tag)。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: PLC0415

    key = _get_key()
    aesgcm = AESGCM(key)
    nonce = os.urandom(_NONCE_LENGTH)
    return base64.b64encode(
        nonce + aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None),
    ).decode("ascii")


def decrypt_pii(ciphertext_b64: str) -> str:
    """解密 base64(nonce + ciphertext + tag) → 明文。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: PLC0415

    key = _get_key()
    raw = base64.b64decode(ciphertext_b64)
    nonce = raw[:_NONCE_LENGTH]
    ct = raw[_NONCE_LENGTH:]
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ct, None).decode("utf-8")


def maybe_decrypt(value: str | None) -> str:
    """解密 PII 字段，未加密（或解密失败）时原样返回。"""
    if not value:
        return ""
    if len(value) > 24 and all(
        c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
        for c in value
    ):
        try:
            return decrypt_pii(value)
        except Exception:
            return value
    return value
