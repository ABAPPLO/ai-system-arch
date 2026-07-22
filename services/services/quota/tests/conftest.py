"""共享 fixtures（quota tests）。"""

import os

# Settings 最小环境变量。Redis 指 dev 的 apihub-redis（:16380）——
# quota 限流器用 Lua EVAL,fakeredis 2.36 不支持,须真 Redis（镜像 test_db_rls 的真 PG 模式）。
_ENV_DEFAULTS = {
    "PG_HOST": "localhost",
    "PG_USER": "apihub",
    "PG_PASSWORD": "test",
    "REDIS_HOST": "localhost",
    "REDIS_PORT": "16380",
    "REDIS_PASSWORD": "apihub_dev_pwd",
    "ENV": "test",
    "OTEL_SERVICE_NAME": "quota-test",
}
for _k, _v in _ENV_DEFAULTS.items():
    os.environ.setdefault(_k, _v)

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
async def fake_redis():
    """真 Redis（apihub-redis）—— 原生支持 Lua EVAL（fakeredis 2.36 不支持）。

    每 test FLUSHDB 隔离；Redis 或 Lua EVAL 不可用则 skip（同 test_db_rls 真栈模式）。
    """
    from apihub_core import redis as redis_mod

    await redis_mod.init_redis(get_settings())
    client = redis_mod.raw_client()
    try:
        await client.ping()
        await client.eval("return 1", 0)  # 探 Lua EVAL 支持
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Redis (Lua eval) unavailable — run `docker compose up -d redis`: {e}")
    await client.flushdb()
    yield client
    await client.flushdb()  # 清理
