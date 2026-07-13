"""AES-256-GCM 加解密 —— Provider API Key 加密存储。

密钥来源：环境变量 AI_GATEWAY_ENCRYPTION_KEY（32 字节 hex 字符串）。
密文格式：base64(nonce + ciphertext + tag)。
"""

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from apihub_core.config import get_settings

_NONCE_LENGTH = 12  # AES-GCM 推荐 96-bit nonce


def _get_key() -> bytes:
    key_hex = get_settings().ai_gateway_encryption_key
    if not key_hex:
        raise RuntimeError("AI_GATEWAY_ENCRYPTION_KEY not configured")
    return bytes.fromhex(key_hex)


def encrypt(plaintext: str) -> str:
    """加密明文 → base64(nonce + ciphertext + tag)。"""
    key = _get_key()
    aesgcm = AESGCM(key)
    nonce = os.urandom(_NONCE_LENGTH)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(nonce + ciphertext).decode("ascii")


def decrypt(ciphertext_b64: str) -> str:
    """解密 base64(nonce + ciphertext + tag) → 明文。"""
    key = _get_key()
    raw = base64.b64decode(ciphertext_b64)
    nonce = raw[:_NONCE_LENGTH]
    ct = raw[_NONCE_LENGTH:]
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ct, None).decode("utf-8")
