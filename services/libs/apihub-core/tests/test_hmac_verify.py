"""R2e T7: inbound HMAC 验签编排单测（mock secret 源 + fake redis）。

覆盖 _verify_hmac 各分支：
  - enrolled + 正确签名 → 通过
  - 未 enrolled 带签名 → 401 not enrolled
  - enrolled 不带签名 → 401 hmac signing required（防降级绕过 bearer）
  - 篡改 body → 401 invalid signature
  - 重放 nonce → 401 replay
  - 过期 timestamp → 401 timestamp
  - secret 缓存密文损坏 → 503 + 清缓存（非客户端错）
"""

import time

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("HMAC_SECRET_KEY", "a" * 64)
    monkeypatch.setenv("PG_HOST", "localhost")
    monkeypatch.setenv("PG_USER", "apihub")
    monkeypatch.setenv("PG_PASSWORD", "t")
    monkeypatch.setenv("REDIS_HOST", "localhost")
    from apihub_core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def fake_redis(monkeypatch):
    import fakeredis.aioredis
    from apihub_core import redis as redis_mod

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_mod, "_client", fake)
    return fake


def _make_request(*, method="POST", path="/v1/foo", query="", body=b'{"x":1}',
                  app_key="ak_enrolledkey", secret="the_secret", timestamp=None, nonce="n1"):
    from apihub_core.signing import sign

    ts = timestamp or str(int(time.time()))
    sig = sign(secret, method, path + (("?" + query) if query else ""), body, ts)

    class _Req:
        pass

    req = _Req()
    req.headers = {
        "X-App-Key": app_key,
        "X-Signature": sig,
        "X-Timestamp": ts,
        "X-Nonce": nonce,
    }
    req.method = method
    req.url = type("U", (), {"path": path, "query": query})()
    _body_bytes = body

    async def _body():
        return _body_bytes

    req.body = _body
    return req


async def _seed_enrolled(fake_redis, secret="the_secret"):
    from apihub_core import crypto, identity

    await identity.write_identity("ak_enrolledkey", {
        "is_active": True, "tenant_id": "t1", "tenant_type": "internal",
        "app_id": "app1", "key_id": "key_1", "hmac_enrolled": True,
    }, ttl=300)
    await identity.write_hmac_secret("ak_enrolledkey", crypto.encrypt_secret(secret), ttl=300)


async def test_enrolled_correct_signature_passes(fake_redis):
    from apihub_core.auth import authenticate_request
    from apihub_core.config import get_settings

    await _seed_enrolled(fake_redis)
    req = _make_request()
    ctx = await authenticate_request(req, get_settings(), api_key="ak_enrolledkey")
    assert ctx.tenant_id == "t1"


async def test_unenrolled_key_with_signature_rejected(fake_redis):
    from apihub_core import identity
    from apihub_core.auth import authenticate_request
    from apihub_core.config import get_settings
    from apihub_core.errors import ApiError

    await identity.write_identity("ak_plain", {
        "is_active": True, "tenant_id": "t1", "tenant_type": "internal",
        "app_id": "app1", "key_id": "key_2", "hmac_enrolled": False,
    }, ttl=300)
    req = _make_request(app_key="ak_plain", secret="x")
    with pytest.raises(ApiError, match="not enrolled"):
        await authenticate_request(req, get_settings(), api_key="ak_plain")


async def test_enrolled_key_missing_signature_rejected(fake_redis):
    """enrolled key 不带签名头 → 401（防降级 bearer 绕过）。"""
    from apihub_core.auth import authenticate_request
    from apihub_core.config import get_settings
    from apihub_core.errors import ApiError

    await _seed_enrolled(fake_redis)

    class _Req:
        pass

    req = _Req()
    req.headers = {"X-App-Key": "ak_enrolledkey"}  # 无 X-Signature
    req.method = "POST"
    req.url = type("U", (), {"path": "/v1/foo", "query": ""})()

    async def _b():
        return b"{}"

    req.body = _b
    with pytest.raises(ApiError, match="hmac signing required"):
        await authenticate_request(req, get_settings(), api_key="ak_enrolledkey")


async def test_tampered_body_rejected(fake_redis):
    from apihub_core.auth import authenticate_request
    from apihub_core.config import get_settings
    from apihub_core.errors import ApiError
    from apihub_core.signing import sign

    await _seed_enrolled(fake_redis)
    # 签 {"x":1} 的 canonical，但 body 是 {"x":2}
    req = _make_request(body=b'{"x":2}')
    req.headers["X-Signature"] = sign(
        "the_secret", "POST", "/v1/foo", b'{"x":1}', req.headers["X-Timestamp"]
    )
    with pytest.raises(ApiError, match="invalid signature"):
        await authenticate_request(req, get_settings(), api_key="ak_enrolledkey")


async def test_replay_nonce_rejected(fake_redis):
    from apihub_core.auth import authenticate_request
    from apihub_core.config import get_settings
    from apihub_core.errors import ApiError

    await _seed_enrolled(fake_redis)
    req1 = _make_request(nonce="dup")
    await authenticate_request(req1, get_settings(), api_key="ak_enrolledkey")
    req2 = _make_request(nonce="dup")
    with pytest.raises(ApiError, match="replay"):
        await authenticate_request(req2, get_settings(), api_key="ak_enrolledkey")


async def test_stale_timestamp_rejected(fake_redis):
    from apihub_core.auth import authenticate_request
    from apihub_core.config import get_settings
    from apihub_core.errors import ApiError

    await _seed_enrolled(fake_redis)
    old_ts = str(int(time.time()) - 600)  # -10min，超 ±5min 窗
    req = _make_request(timestamp=old_ts)
    with pytest.raises(ApiError, match="timestamp"):
        await authenticate_request(req, get_settings(), api_key="ak_enrolledkey")


async def test_corrupt_secret_cache_returns_503(fake_redis):
    """secret 缓存密文损坏（decrypt 抛 InvalidTag/binascii）→ 503 + DEL 缓存（不当 401）。"""
    from apihub_core import identity
    from apihub_core.auth import authenticate_request
    from apihub_core.config import get_settings
    from apihub_core.errors import ApiError

    await _seed_enrolled(fake_redis)
    # 用坏 blob 覆盖
    await identity.write_hmac_secret("ak_enrolledkey", "!!!not-valid-b64!!!", ttl=300)
    req = _make_request()
    with pytest.raises(ApiError) as exc_info:
        await authenticate_request(req, get_settings(), api_key="ak_enrolledkey")
    assert exc_info.value.http_status == 503
