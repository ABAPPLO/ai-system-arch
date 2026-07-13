"""jwt_utils 单测 —— 签发/验签/格式判定。"""

import pytest
from apihub_core import jwt_utils


def test_issue_and_decode_roundtrip():
    token = jwt_utils.issue_token(
        user_id="u_1", tenant_id="external-public", secret="s", ttl_seconds=60
    )
    assert jwt_utils.is_jwt(token) is True
    payload = jwt_utils.decode_token(token, "s")
    assert payload["user_id"] == "u_1"
    assert payload["tenant_id"] == "external-public"
    assert payload["is_platform_admin"] is False


def test_decode_wrong_secret_raises():
    token = jwt_utils.issue_token(user_id="u", tenant_id="t", secret="s", ttl_seconds=60)
    with pytest.raises(jwt_utils.JWTError):
        jwt_utils.decode_token(token, "other-secret")


def test_is_jwt_false_for_apikey():
    assert jwt_utils.is_jwt("ak_abcdef123456") is False
    assert jwt_utils.is_jwt("") is False
