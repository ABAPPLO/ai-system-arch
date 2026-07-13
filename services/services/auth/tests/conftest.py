"""共享 fixtures（auth tests）。"""

import os

# 在任何 test module import apihub_core 之前注入最小环境变量，
# 这样 auth.main → create_app → get_settings() 不会因为缺 PG_HOST 等报错。
# 真正的 PG/Redis 连接只在 integration 测试里用到。
_ENV_DEFAULTS = {
    "PG_HOST": "localhost",
    "PG_USER": "apihub",
    "PG_PASSWORD": "test",
    "REDIS_HOST": "localhost",
    "REDIS_PORT": "6379",
    "ENV": "test",
    "OTEL_SERVICE_NAME": "auth-test",
}
for k, v in _ENV_DEFAULTS.items():
    os.environ.setdefault(k, v)

import pytest  # noqa: E402
from apihub_core.config import get_settings  # noqa: E402
from apihub_core.tenant import clear_tenant_context  # noqa: E402


@pytest.fixture(autouse=True)
def reset_tenant_context():
    clear_tenant_context()
    yield
    clear_tenant_context()


@pytest.fixture
def tenant_a():
    from apihub_core.tenant import TenantContext

    return TenantContext(
        tenant_id="tenant_a",
        tenant_type="internal",
        app_id="app_trading",
    )


@pytest.fixture
def tenant_admin():
    from apihub_core.tenant import TenantContext

    return TenantContext(
        tenant_id="",
        tenant_type="system",
        is_platform_admin=True,
    )


@pytest.fixture(autouse=True)
def clear_settings_cache():
    """每个测试都重新构造 Settings，避免上一个测试的 monkeypatch 残留。"""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def fake_redis(monkeypatch):
    """用 fakeredis 替换 apihub_core.redis._client（镜像 admin tests conftest）。

    identity 注册/验证/登录的 Redis 操作（t:verify:{token}）落 FakeRedis，
    不依赖真 Redis。无 tenant context → key 不加前缀，原样存取。
    """
    import fakeredis.aioredis
    from apihub_core import redis as redis_mod

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_mod, "_client", fake)
    return fake
