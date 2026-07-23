"""钉钉 OAuth2 客户端（admin SSO）。

真实分支打 DingTalk OAuth2 端点；dingtalk_mock_mode=true 时走 mock 协议
（code/access_token 形如 mock:<unionId>:<name> / mock-token:<unionId>:<name>），
让 dev/kind 全链 e2e 免真实钉钉应用。
"""

from __future__ import annotations

import httpx
from apihub_core.errors import ApiError, ErrorCode

_AUTHORIZE_BASE = "https://login.dingtalk.com/oauth2/auth"
_TOKEN_URL = "https://api.dingtalk.com/v1.0/oauth2/userAccessToken"  # noqa: S105  URL 常量非密钥
_USERINFO_URL = "https://api.dingtalk.com/v1.0/contact/users/me"


def build_authorize_url(*, client_id: str, redirect_uri: str, state: str) -> str:
    """构造钉钉扫码授权 URL（response_type=code, scope=openid, prompt=consent）。"""
    from urllib.parse import urlencode

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid",
        "state": state,
        "prompt": "consent",
    }
    return f"{_AUTHORIZE_BASE}?{urlencode(params)}"


async def exchange_code_for_token(*, settings, code: str) -> str:
    """code → userAccessToken。mock 模式直接透传解析。"""
    if settings.dingtalk_mock_mode:
        return _mock_token_from_code(code)
    async with httpx.AsyncClient(timeout=10.0) as c:
        resp = await c.post(
            _TOKEN_URL,
            json={
                "clientId": settings.dingtalk_client_id,
                "clientSecret": settings.dingtalk_client_secret,
                "grantType": "authorization_code",
                "code": code,
            },
        )
    if resp.status_code != 200:
        raise ApiError(
            ErrorCode.UNAUTHORIZED,
            f"dingtalk token exchange failed: {resp.status_code}",
            http_status=401,
        )
    token = resp.json().get("accessToken")
    if not token:
        raise ApiError(
            ErrorCode.UNAUTHORIZED, "dingtalk token exchange: empty accessToken", http_status=401
        )
    return token


async def fetch_userinfo(*, settings, access_token: str) -> dict:
    """userAccessToken → {union_id, name}。mock 模式按 token 解析。"""
    if settings.dingtalk_mock_mode:
        return _mock_userinfo_from_token(access_token)
    async with httpx.AsyncClient(timeout=10.0) as c:
        resp = await c.get(_USERINFO_URL, headers={"x-acs-dingtalk-access-token": access_token})
    if resp.status_code != 200:
        raise ApiError(
            ErrorCode.UNAUTHORIZED, f"dingtalk userinfo failed: {resp.status_code}", http_status=401
        )
    data = resp.json()
    union_id = data.get("unionId")
    if not union_id:
        raise ApiError(
            ErrorCode.UNAUTHORIZED, "dingtalk userinfo: missing unionId", http_status=401
        )
    return {"union_id": union_id, "name": data.get("nick") or "DingTalk User"}


# ---------- mock 协议（仅 dingtalk_mock_mode=true）----------

_CODE_PREFIX = "mock:"
_TOKEN_PREFIX = "mock-token:"  # noqa: S105  协议前缀非密钥


def _mock_token_from_code(code: str) -> str:
    if not code.startswith(_CODE_PREFIX):
        raise ApiError(
            ErrorCode.INVALID_INPUT, "mock code must be 'mock:<unionId>:<name>'", http_status=400
        )
    return _TOKEN_PREFIX + code[len(_CODE_PREFIX) :]


def _mock_userinfo_from_token(token: str) -> dict:
    if not token.startswith(_TOKEN_PREFIX):
        raise ApiError(ErrorCode.UNAUTHORIZED, "invalid mock token", http_status=401)
    payload = token[len(_TOKEN_PREFIX) :]
    parts = payload.split(":", 1)
    union_id = parts[0]
    name = parts[1] if len(parts) > 1 else "Mock User"
    return {"union_id": union_id, "name": name}
