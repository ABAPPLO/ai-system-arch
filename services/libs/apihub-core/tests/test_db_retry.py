"""init_pool 启动建连退避重试测试（kind CNI/DNS 抢跑场景）。"""

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k, v in {
        "PG_HOST": "localhost",
        "PG_USER": "apihub",
        "PG_PASSWORD": "test",
        "REDIS_HOST": "localhost",
        "ENV": "test",
        "PG_POOL_MIN": "1",
        "STARTUP_CONNECT_RETRIES": "5",
        "STARTUP_CONNECT_BACKOFF": "0",  # 测试不真睡
    }.items():
        monkeypatch.setenv(k, v)
    from apihub_core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class _FakeConn:
    async def close(self):
        pass


class _FakePool:
    def __init__(self):
        self.closed = False

    async def acquire(self):
        return _FakeConn()

    async def release(self, conn):  # noqa: ARG002
        pass

    async def close(self):
        self.closed = True


async def test_init_pool_retries_then_succeeds(monkeypatch):
    import asyncpg
    from apihub_core import db

    db._pool = None
    calls = {"n": 0}

    async def _create_pool(**kw):  # noqa: ARG001
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError("Temporary failure in name resolution")
        return _FakePool()

    monkeypatch.setattr(asyncpg, "create_pool", _create_pool)
    from apihub_core.config import get_settings

    await db.init_pool(get_settings())
    assert calls["n"] == 3  # 2 DNS 失败 + 1 成功
    assert db._pool is not None
    await db.close_pool()


async def test_init_pool_exhausts_retries_and_raises(monkeypatch):
    import asyncpg
    from apihub_core import db

    db._pool = None
    calls = {"n": 0}

    async def _create_pool(**kw):  # noqa: ARG001
        calls["n"] += 1
        raise OSError("Temporary failure in name resolution")

    monkeypatch.setattr(asyncpg, "create_pool", _create_pool)
    from apihub_core.config import get_settings

    with pytest.raises(OSError):
        await db.init_pool(get_settings())
    assert calls["n"] == 5  # 耗尽 STARTUP_CONNECT_RETRIES
    assert db._pool is None  # 失败不残留 pool
