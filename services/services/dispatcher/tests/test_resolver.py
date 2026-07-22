"""resolver path 匹配测试 —— 纯函数，不依赖 PG。

注：resolve_by_path / _match_path 已于 R1c §3 移除（dispatcher 退纯转发，
强制 X-API-Version-Id）。保留 _extract_path_params 的单测。
R3e T5: 追加 snapshot L1 命中跳过 Redis 的测试。
"""

import dataclasses

import pytest
from dispatcher.resolver import _extract_path_params


@pytest.fixture(autouse=True)
def _reset_snapshot_l1():
    """L1 是 opt-in（默认 None）；任何测试置位后强制复位，防泄漏。"""
    from dispatcher import resolver

    yield
    resolver.configure_snapshot_l1(None)


class TestExtractPathParams:
    def test_single_var(self):
        params = _extract_path_params("/v1/users/{user_id}", "/v1/users/u_001")
        assert params == {"user_id": "u_001"}

    def test_multiple_vars(self):
        params = _extract_path_params(
            "/v1/orders/{order_id}/items/{item_id}",
            "/v1/orders/o_001/items/i_042",
        )
        assert params == {"order_id": "o_001", "item_id": "i_042"}

    def test_no_vars(self):
        params = _extract_path_params("/v1/health", "/v1/health")
        assert params == {}

    def test_var_captures_any_segment(self):
        # 段内可含字母数字 / dash
        params = _extract_path_params("/v1/users/{user_id}", "/v1/users/abc-123_XYZ")
        assert params == {"user_id": "abc-123_XYZ"}


async def test_snapshot_l1_hit_skips_redis(monkeypatch):
    """resolve_by_header L1 命中 → 不读 Redis（t_get 计数为 0）。

    L1 存 dict（= asdict(snapshot)，即 _from_json 直接消费的形态）。
    L1 命中直接返回 _from_json(hit)，既绕过 Redis，也绕过 Redis-hit 路径的
    PK status 兜底查询 —— 这是 opt-in L1 用 TTL 削峰换来的已知 staleness 窗口。
    """
    from apihub_core import redis as redis_mod
    from apihub_core.l1 import TTLCache
    from dispatcher import resolver

    resolver.configure_snapshot_l1(TTLCache(maxsize=8, ttl=5))
    snap = resolver._from_json(
        {
            "id": "ver_l1",
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
    )
    # L1 缓存 asdict(snapshot) —— 与 Redis t_set 存 json.dumps(asdict(...)) 同源，
    # 也是 _from_json 直接可消费的 dict 形态。
    resolver._snapshot_l1.set("snapshot:ver_l1", dataclasses.asdict(snap))

    t_get_calls = {"n": 0}

    async def _spy_t_get(key):
        t_get_calls["n"] += 1
        return None  # 即便意外落到 Redis 也返回 miss，迫使后续 DB 查报错暴露问题

    monkeypatch.setattr(redis_mod, "t_get", _spy_t_get)

    out = await resolver.resolve_by_header("ver_l1")
    assert out.id == "ver_l1"
    assert out.api_id == "api_1"
    assert t_get_calls["n"] == 0  # L1 命中 → Redis 未读
