"""Settings 默认值 / env 覆盖测试。"""

from apihub_core.config import Settings, get_settings

# Settings 必填字段（无默认）：pg_host / pg_user / pg_password / redis_host
_REQUIRED = {
    "pg_host": "localhost",
    "pg_user": "apihub",
    "pg_password": "test",  # noqa: S106
    "redis_host": "localhost",
}


def test_pg_ssl_default_is_prefer():
    """dev 默认 prefer（先试 SSL，无则明文）；prod 由 env 显式覆盖。"""
    s = Settings(**_REQUIRED)
    assert s.pg_ssl == "prefer"


def test_pg_ssl_env_override(monkeypatch):
    monkeypatch.setenv("PG_SSL", "verify-full")
    get_settings.cache_clear()
    assert Settings(**_REQUIRED).pg_ssl == "verify-full"
    get_settings.cache_clear()


def test_executor_port_default():
    s = Settings(**_REQUIRED)
    assert s.executor_port == 8003


def test_executor_port_env_override(monkeypatch):
    monkeypatch.setenv("EXECUTOR_PORT", "9000")
    get_settings.cache_clear()
    assert Settings(**_REQUIRED).executor_port == 9000
    get_settings.cache_clear()
