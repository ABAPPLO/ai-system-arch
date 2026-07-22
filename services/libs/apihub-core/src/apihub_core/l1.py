"""进程内 TTL+LRU 缓存 —— dispatcher L1（短 TTL 削峰，Redis 为真相源）。

单 asyncio 事件循环协作式访问 → 无锁。maxsize 防 unbounded（LRU 淘汰最老）。
仅缓存「数据」：caller 仍每请求跑鉴权决策（enrolled/verify/nonce/replay）。
"""

import time
from collections import OrderedDict
from typing import Any


class TTLCache:
    def __init__(self, maxsize: int = 4096, ttl: float = 5.0) -> None:
        self._maxsize = maxsize
        self._ttl = ttl
        self._store: OrderedDict[str, tuple[float, Any]] = OrderedDict()

    def get(self, key: str) -> Any | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if time.monotonic() > expires_at:
            self._store.pop(key, None)
            return None
        self._store.move_to_end(key)  # LRU：最近访问后移
        return value

    def set(self, key: str, value: Any) -> None:
        self._store[key] = (time.monotonic() + self._ttl, value)
        self._store.move_to_end(key)
        while len(self._store) > self._maxsize:
            self._store.popitem(last=False)  # 淘汰最老

    def invalidate(self, key: str) -> None:
        self._store.pop(key, None)

    def clear(self) -> None:
        self._store.clear()
