"""共享 fixtures（quota tests）。"""

import os

# Settings 最小环境变量（同 auth/executor 模式）
_ENV_DEFAULTS = {
    "PG_HOST": "localhost",
    "PG_USER": "apihub",
    "PG_PASSWORD": "test",
    "REDIS_HOST": "localhost",
    "REDIS_PORT": "6379",
    "ENV": "test",
    "OTEL_SERVICE_NAME": "quota-test",
}
for k, v in _ENV_DEFAULTS.items():
    os.environ.setdefault(k, v)

import pytest  # noqa: E402
from apihub_core.config import get_settings  # noqa: E402
from apihub_core.tenant import clear_tenant_context  # noqa: E402


@pytest.fixture(autouse=True)
def reset_state():
    clear_tenant_context()
    get_settings.cache_clear()
    yield
    clear_tenant_context()
    get_settings.cache_clear()


@pytest.fixture
def tenant_a():
    from apihub_core.tenant import TenantContext
    return TenantContext(
        tenant_id="tenant_a",
        tenant_type="internal",
        app_id="app_trading",
    )


@pytest.fixture
def fake_redis(monkeypatch):
    """用 fakeredis 替换全局 redis client（支持 eval Lua）。"""
    import fakeredis.aioredis
    from apihub_core import redis as redis_mod

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_mod, "_client", fake)
    return fake
