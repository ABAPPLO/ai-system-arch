"""resolve_by_header 生命周期映射 + /dispatch 强制 header。"""

import json

import pytest
from apihub_core.errors import ApiError, ErrorCode


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch):
    from apihub_core import redis as redis_mod

    async def _miss(key):
        return None

    async def _noop_set(key, value, ex=None):
        return None

    monkeypatch.setattr(redis_mod, "t_get", _miss)
    monkeypatch.setattr(redis_mod, "t_set", _noop_set)
    yield


async def _row(status):
    return {
        "id": "ver_1",
        "api_id": "api_1",
        "tenant_id": "t1",
        "version": "v1",
        "backend_type": "http",
        "backend_url": "http://up/v1",
        "method": "GET",
        "path": "/x",
        "masking": None,
        "rate_limit": None,
        "retry_policy": None,
        "cache_policy": None,
        "ai_model": None,
        "ai_streaming": False,
        "ai_params": None,
        "sla_p99_ms": None,
        "sla_availability": None,
        "status": status,
    }


def _meta_session(fetchrow):
    class _CM:
        async def __aenter__(self):
            return type(
                "C",
                (),
                {
                    "fetchrow": staticmethod(fetchrow),
                    "fetchval": staticmethod(lambda *a, **k: None),
                },
            )()

        async def __aexit__(self, *e):
            return False

    def _factory(*a, **k):
        return _CM()

    return _factory


async def test_resolve_published_ok(monkeypatch):
    from dispatcher import resolver

    async def _fr(sql, *a):
        return await _row("published")

    monkeypatch.setattr(resolver.db, "meta_db_session", _meta_session(_fr))
    monkeypatch.setattr(resolver, "_get_api_meta", lambda api_id: _pub_pair())
    snap = await resolver.resolve_by_header("ver_1")
    assert snap.id == "ver_1"


async def _pub_pair():
    return ("/v1", "public")


async def test_resolve_deprecated_ok(monkeypatch):
    from dispatcher import resolver

    async def _fr(sql, *a):
        return await _row("deprecated")

    monkeypatch.setattr(resolver.db, "meta_db_session", _meta_session(_fr))
    monkeypatch.setattr(resolver, "_get_api_meta", lambda api_id: _pub_pair())
    snap = await resolver.resolve_by_header("ver_1")
    assert snap.id == "ver_1"


async def test_resolve_retired_returns_410(monkeypatch):
    from dispatcher import resolver

    async def _fr(sql, *a):
        if "IN ('published', 'deprecated')" in sql:
            return None
        return await _row("retired")

    async def _fval(sql, *a):
        return "retired"

    class _CM:
        async def __aenter__(self):
            return type("C", (), {"fetchrow": staticmethod(_fr), "fetchval": staticmethod(_fval)})()

        async def __aexit__(self, *e):
            return False

    def _factory(*a, **k):
        return _CM()

    monkeypatch.setattr(resolver.db, "meta_db_session", _factory)
    with pytest.raises(ApiError) as ei:
        await resolver.resolve_by_header("ver_1")
    assert ei.value.code == ErrorCode.API_RETIRED
    assert ei.value.http_status == 410


async def test_dispatch_missing_header_returns_400(async_client):
    """resolve_by_path 已删；无 X-API-Version-Id → 400（需 X-API-Key 过 auth）。"""
    r = await async_client.get("/dispatch/v1/x", headers={"X-API-Key": "ak_test_a_demo001"})
    assert r.status_code == 400


async def test_resolve_by_path_removed():
    from dispatcher import resolver

    assert not hasattr(resolver, "resolve_by_path")


async def test_resolve_cache_hit_stale_retired_returns_410(monkeypatch):
    """缓存命中但版本已 retire → 410，且清 stale 缓存（t_delete 被调）。

    cached snapshot 无 status 字段，retire 后最多 5 分钟仍命中 stale 缓存 →
    命中时用 PK 状态查询兜底：retired→410 并删缓存键。
    """
    from apihub_core import redis as redis_mod
    from dispatcher import resolver

    cached_snapshot = {
        "id": "ver_1",
        "api_id": "api_1",
        "tenant_id": "t1",
        "version": "v1",
        "backend_type": "http",
        "backend_url": "http://up/v1",
        "method": "GET",
        "path": "/x",
        "masking": None,
        "rate_limit": None,
        "retry_policy": None,
        "cache_policy": None,
        "ai_model": None,
        "ai_streaming": False,
        "ai_params": None,
        "sla_p99_ms": None,
        "sla_availability": None,
        "timeout_ms": 30000,
        "visibility": "public",
    }

    # 覆盖 autouse _no_cache：让 t_get 命中（返回缓存快照 JSON）。
    async def _hit(key):
        return json.dumps(cached_snapshot)

    monkeypatch.setattr(redis_mod, "t_get", _hit)

    # 跟踪 t_delete 调用（autouse 未 patch t_delete，真实 redis 未初始化会抛）。
    deleted: list[str] = []

    async def _delete(key):
        deleted.append(key)

    monkeypatch.setattr(redis_mod, "t_delete", _delete)

    # meta_db_session.fetchval → "retired"（版本已退役）。
    async def _fval(sql, *a):
        return "retired"

    class _CM:
        async def __aenter__(self):
            return type("C", (), {"fetchval": staticmethod(_fval)})()

        async def __aexit__(self, *e):
            return False

    def _factory(*a, **k):
        return _CM()

    monkeypatch.setattr(resolver.db, "meta_db_session", _factory)

    with pytest.raises(ApiError) as ei:
        await resolver.resolve_by_header("ver_1")
    assert ei.value.code == ErrorCode.API_RETIRED
    assert ei.value.http_status == 410
    # stale 缓存键被清。
    assert "snapshot:ver_1" in deleted


async def test_resolve_cache_hit_stale_other_status_returns_404(monkeypatch):
    """缓存命中但版本 status 既非 published/deprecated、也非 retired → 404 并清缓存。"""
    from apihub_core import redis as redis_mod
    from dispatcher import resolver

    cached_snapshot = {
        "id": "ver_2",
        "api_id": "api_1",
        "tenant_id": "t1",
        "version": "v1",
        "backend_type": "http",
        "backend_url": "http://up/v1",
        "method": "GET",
        "path": "/x",
        "masking": None,
        "rate_limit": None,
        "retry_policy": None,
        "cache_policy": None,
        "ai_model": None,
        "ai_streaming": False,
        "ai_params": None,
        "sla_p99_ms": None,
        "sla_availability": None,
        "timeout_ms": 30000,
        "visibility": "public",
    }

    async def _hit(key):
        return json.dumps(cached_snapshot)

    monkeypatch.setattr(redis_mod, "t_get", _hit)

    deleted: list[str] = []

    async def _delete(key):
        deleted.append(key)

    monkeypatch.setattr(redis_mod, "t_delete", _delete)

    async def _fval(sql, *a):
        return "draft"

    class _CM:
        async def __aenter__(self):
            return type("C", (), {"fetchval": staticmethod(_fval)})()

        async def __aexit__(self, *e):
            return False

    def _factory(*a, **k):
        return _CM()

    monkeypatch.setattr(resolver.db, "meta_db_session", _factory)

    with pytest.raises(ApiError) as ei:
        await resolver.resolve_by_header("ver_2")
    assert ei.value.code == ErrorCode.API_NOT_PUBLISHED
    assert ei.value.http_status == 404
    assert "snapshot:ver_2" in deleted
