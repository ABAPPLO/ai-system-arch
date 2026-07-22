"""TTL+LRU 进程内缓存单测。"""

import time


def test_set_get_roundtrip():
    from apihub_core.l1 import TTLCache

    c = TTLCache(maxsize=8, ttl=5.0)
    c.set("k", {"x": 1})
    assert c.get("k") == {"x": 1}


def test_miss_returns_none():
    from apihub_core.l1 import TTLCache

    c = TTLCache(maxsize=8, ttl=5.0)
    assert c.get("absent") is None


def test_expiry_returns_none():
    from apihub_core.l1 import TTLCache

    c = TTLCache(maxsize=8, ttl=0.05)
    c.set("k", "v")
    assert c.get("k") == "v"
    time.sleep(0.06)
    assert c.get("k") is None  # 过期


def test_lru_eviction():
    from apihub_core.l1 import TTLCache

    c = TTLCache(maxsize=2, ttl=5.0)
    c.set("a", 1)
    c.set("b", 2)
    assert c.get("a") == 1  # a 最近用
    c.set("c", 3)  # 满 → 淘汰最老 b
    assert c.get("b") is None
    assert c.get("a") == 1
    assert c.get("c") == 3


def test_invalidate_and_clear():
    from apihub_core.l1 import TTLCache

    c = TTLCache(maxsize=8, ttl=5.0)
    c.set("k", "v")
    c.invalidate("k")
    assert c.get("k") is None
    c.set("k2", "v2")
    c.clear()
    assert c.get("k2") is None
