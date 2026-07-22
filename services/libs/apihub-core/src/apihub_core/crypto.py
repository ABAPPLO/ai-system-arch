"""AES-256-GCM 加解密 —— HMAC signing secret 加密存储。

密钥来源：环境变量 HMAC_SECRET_KEY（32 字节 hex 字符串）。
密文格式：base64(nonce + ciphertext + tag)。
与 ai_gateway/crypto.py 同构但独立 env key（爆炸半径隔离）。
"""

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from apihub_core.config import get_settings

_NONCE_LENGTH = 12  # AES-GCM 推荐 96-bit nonce


def _get_key() -> bytes:
    key_hex = get_settings().hmac_secret_key
    if not key_hex:
        raise RuntimeError("HMAC_SECRET_KEY not configured")
    return bytes.fromhex(key_hex)


def encrypt_secret(plaintext: str) -> str:
    """加密明文 → base64(nonce + ciphertext + tag)。"""
    key = _get_key()
    aesgcm = AESGCM(key)
    nonce = os.urandom(_NONCE_LENGTH)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(nonce + ciphertext).decode("ascii")


def decrypt_secret(ciphertext_b64: str) -> str:
    """解密 base64(nonce + ciphertext + tag) → 明文。损坏抛 InvalidTag（上层转 503/401）。"""
    key = _get_key()
    raw = base64.b64decode(ciphertext_b64)
    nonce = raw[:_NONCE_LENGTH]
    ct = raw[_NONCE_LENGTH:]
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ct, None).decode("utf-8")
