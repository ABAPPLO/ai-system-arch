"""R0a: prod 启动断言 —— 拒绝不安全默认密钥。纯单测，无 PG。"""

import pytest
from apihub_core.config import Settings


def _mk(**overrides):
    base = {"pg_host": "x", "pg_user": "x", "pg_password": "x", "redis_host": "x"}
    base.update(overrides)
    return Settings(**base)


def test_raises_in_prod_with_default_secrets(monkeypatch):
    monkeypatch.delenv("REQUIRE_SECURE_SECRETS", raising=False)
    s = _mk(env="prod")  # 三个密钥都是默认值
    with pytest.raises(RuntimeError, match="Insecure default"):
        s.validate_security()


def test_ok_in_dev_with_default_secrets(monkeypatch):
    monkeypatch.delenv("REQUIRE_SECURE_SECRETS", raising=False)
    s = _mk(env="dev")
    s.validate_security()  # 不抛


def test_ok_in_prod_with_custom_secrets(monkeypatch):
    monkeypatch.delenv("REQUIRE_SECURE_SECRETS", raising=False)
    s = _mk(
        env="prod",
        jwt_secret="real-jwt-secret",
        pii_encryption_key="ab" * 32,  # 64 hex = 32 字节
        oss_secret_key="real-oss-secret",
        hmac_secret_key="cd" * 32,  # R2e: HMAC secret 加密 key，prod 必须注入
    )
    s.validate_security()  # 不抛


def test_require_secure_secrets_flag_enforces_in_dev(monkeypatch):
    monkeypatch.setenv("REQUIRE_SECURE_SECRETS", "1")
    s = _mk(env="dev")  # 默认密钥 + 显式 flag
    with pytest.raises(RuntimeError, match="Insecure default"):
        s.validate_security()


def test_dispatcher_l1_defaults(monkeypatch):
    for k in ("DISPATCHER_L1_ENABLED", "DISPATCHER_L1_TTL_SECONDS", "DISPATCHER_L1_MAXSIZE"):
        monkeypatch.delenv(k, raising=False)
    from apihub_core.config import get_settings
    get_settings.cache_clear()
    s = get_settings()
    assert s.dispatcher_l1_enabled is True
    assert s.dispatcher_l1_ttl_seconds == 5.0
    assert s.dispatcher_l1_maxsize == 4096
    get_settings.cache_clear()
