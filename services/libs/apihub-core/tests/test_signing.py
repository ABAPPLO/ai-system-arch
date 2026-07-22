"""HMAC signing 纯函数单测。"""

import hashlib
import hmac as _std_hmac


def test_canonical_string_shape():
    from apihub_core.signing import canonical_string
    body = b'{"x":1}'
    s = canonical_string("POST", "/v1/foo?a=1", body, "1700000000")
    assert s == f"POST\n/v1/foo?a=1\n1700000000\n{hashlib.sha256(body).hexdigest()}"


def test_canonical_body_field_isolation():
    from apihub_core.signing import canonical_string
    a = canonical_string("POST", "/p", b'{"x":1}', "1")
    b = canonical_string("POST", "/p", b'{"x":2}', "1")
    assert a != b


def test_sign_verify_roundtrip_inbound():
    from apihub_core.signing import sign, verify
    sig = sign("secret", "POST", "/v1/foo?a=1", b'{"x":1}', "1700000000")
    assert verify("secret", "POST", "/v1/foo?a=1", b'{"x":1}', "1700000000", sig) is True
    assert verify("secret", "POST", "/v1/foo?a=1", b'{"x":2}', "1700000000", sig) is False


def test_verify_wrong_secret_false():
    from apihub_core.signing import sign, verify
    sig = sign("secret", "POST", "/p", b"b", "1")
    assert verify("other", "POST", "/p", b"b", "1", sig) is False


def test_verify_truncated_signature_false():
    from apihub_core.signing import verify
    assert verify("s", "POST", "/p", b"b", "1", "short") is False


def test_empty_body():
    from apihub_core.signing import canonical_string
    s = canonical_string("GET", "/p", b"", "1")
    assert hashlib.sha256(b"").hexdigest() in s


def test_webhook_sign_verify():
    from apihub_core.signing import sign_webhook, verify_webhook
    body = b'{"event":"x"}'
    sig = sign_webhook("wh_secret", body)
    assert verify_webhook("wh_secret", body, sig) is True
    assert verify_webhook("wh_secret", b'{"event":"y"}', sig) is False


def test_webhook_matches_raw_hmac():
    """outbound 须与现有 consumer.py 的 hmac.new(secret, body, sha256) 逐字节兼容。"""
    from apihub_core.signing import sign_webhook
    body = b'{"event":"x"}'
    expected = _std_hmac.new(b"wh_secret", body, hashlib.sha256).hexdigest()
    assert sign_webhook("wh_secret", body) == expected
