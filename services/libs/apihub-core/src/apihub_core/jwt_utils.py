"""JWT 签发/验签 —— 外部开发者「人」的登录态（HS256）。

与 API Key（机器凭证）分离：JWT 代表开发者这个人，TTL 2h。
"""

import time

import jwt

ALGORITHM = "HS256"


class JWTError(Exception):
    """JWT 验签/解码失败。"""


def is_jwt(token: str) -> bool:
    """粗判：JWT 第一段 base64url 以 'eyJ' 开头。"""
    return bool(token) and token.startswith("eyJ")


def issue_token(
    *,
    user_id: str,
    tenant_id: str,
    secret: str,
    ttl_seconds: int,
    is_platform_admin: bool = False,
) -> str:
    payload = {
        "user_id": user_id,
        "tenant_id": tenant_id,
        "is_platform_admin": is_platform_admin,
        "iat": int(time.time()),
        "exp": int(time.time()) + ttl_seconds,
    }
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def issue_refresh_token(
    *,
    user_id: str,
    tenant_id: str,
    secret: str,
    ttl_seconds: int,
) -> str:
    """签发 refresh token（含唯一 jti，用于 Redis 吊销追踪）。"""
    import uuid

    payload = {
        "user_id": user_id,
        "tenant_id": tenant_id,
        "type": "refresh",
        "jti": uuid.uuid4().hex,
        "iat": int(time.time()),
        "exp": int(time.time()) + ttl_seconds,
    }
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def decode_token(token: str, secret: str) -> dict:
    try:
        return jwt.decode(token, secret, algorithms=[ALGORITHM])
    except jwt.PyJWTError as e:
        raise JWTError(str(e)) from e
