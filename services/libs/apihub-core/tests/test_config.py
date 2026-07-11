"""Settings 默认值 / env 覆盖测试。"""

from apihub_core.config import Settings, get_settings

# Settings 必填字段（无默认）：pg_host / pg_user / pg_password / redis_host
_REQUIRED = {
    "pg_host": "localhost",
    "pg_user": "apihub",
    "pg_password": "test",  # noqa: S106
    "redis_host": "localhost",
}


def test_pg_ssl_default_is_prefer(monkeypatch):
    """dev 默认 prefer（先试 SSL，无则明文）；prod 由 env 显式覆盖。"""
    monkeypatch.delenv("PG_SSL", raising=False)
    get_settings.cache_clear()
    s = Settings(**_REQUIRED)
    assert s.pg_ssl == "prefer"


def test_pg_ssl_env_override(monkeypatch):
    monkeypatch.setenv("PG_SSL", "verify-full")
    get_settings.cache_clear()
    assert Settings(**_REQUIRED).pg_ssl == "verify-full"
    get_settings.cache_clear()


# executor_port 的 env 名是 EXECUTOR_APP_PORT（Field validation_alias），不是 EXECUTOR_PORT。
# #14 (d62514c) 改的：k8s 会自动注入 EXECUTOR_PORT=tcp://<executor-svc>:80（Service 发现），
# 若字段直接读 EXECUTOR_PORT 会被这个值冲坏（pydantic 把 "tcp://..." 当 int 解析崩），
# 故 alias 到 EXECUTOR_APP_PORT 避开。测试须用真实 env 名。


def test_executor_port_default(monkeypatch):
    monkeypatch.delenv("EXECUTOR_APP_PORT", raising=False)
    get_settings.cache_clear()
    s = Settings(**_REQUIRED)
    assert s.executor_port == 8003


def test_executor_port_env_override(monkeypatch):
    monkeypatch.setenv("EXECUTOR_APP_PORT", "9000")
    get_settings.cache_clear()
    assert Settings(**_REQUIRED).executor_port == 9000
    get_settings.cache_clear()
