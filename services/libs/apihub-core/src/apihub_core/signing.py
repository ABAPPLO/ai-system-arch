"""HMAC 签名纯函数 —— inbound 请求验签 + outbound webhook 签名，单一真相源。

inbound canonical（§7.3）：
    canonical = f"{method}\n{raw_path_with_query}\n{timestamp}\n{sha256(body).hexdigest()}"
    signature = HMAC-SHA256(secret, canonical).hexdigest()

outbound（webhook body 签名，client 验）：
    signature = HMAC-SHA256(secret, body).hexdigest()   # body-only canonical

query 保持 client wire 原样（percent-encoded，不 normalize/re-encode）。
所有比对走 hmac.compare_digest（常时）。
"""

import hashlib
import hmac


def canonical_string(method: str, raw_path_with_query: str, body: bytes, timestamp: str) -> str:
    return f"{method}\n{raw_path_with_query}\n{timestamp}\n{hashlib.sha256(body).hexdigest()}"


def sign(secret: str, method: str, raw_path_with_query: str, body: bytes, timestamp: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        canonical_string(method, raw_path_with_query, body, timestamp).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify(
    secret: str,
    method: str,
    raw_path_with_query: str,
    body: bytes,
    timestamp: str,
    provided: str,
) -> bool:
    expected = sign(secret, method, raw_path_with_query, body, timestamp)
    return hmac.compare_digest(expected, provided)


def sign_webhook(secret: str, body: bytes) -> str:
    """outbound：HMAC-SHA256 over raw body（与既有 consumer.py 兼容）。"""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_webhook(secret: str, body: bytes, provided: str) -> bool:
    return hmac.compare_digest(sign_webhook(secret, body), provided)
