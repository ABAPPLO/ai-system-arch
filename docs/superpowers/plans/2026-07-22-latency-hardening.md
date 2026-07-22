# Latency Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 dispatcher 热路径每请求 Redis RTT 从 2 降到 0(L1 命中)/1(miss),HMAC 暖路径从 3 降到 1,不牺牲 R2e HMAC 安全语义。

**Architecture:** dispatcher 进程内 TTL=5s L1 缓存 identity/snapshot **数据**(Redis 仍为真相源,鉴权决策每请求照跑);HMAC 同函数内 identity+hmac_secret 两读合并 pipeline;bearer 跨边界 pipelining defer(L1 已压到 0 RTT);httpx 冷路径共享 client。

**Tech Stack:** Python 3.11 / FastAPI / asyncpg / redis-py async / pytest (asyncio_mode=auto)

**Spec:** `docs/superpowers/specs/2026-07-22-latency-hardening-design.md`

## Global Constraints

- **Python 3.11**;**asyncpg 直连**;jsonb codec 已注册。
- **多租户 + RLS 不变量**不变;L1 只缓存数据,不碰鉴权决策。
- **L1 落点 dispatcher 进程内**;`apihub_core` 的 L1 hook **opt-in 默认关**(其他服务不注入即无变化)。
- **L1 TTL=5s**,接受 revoke/rotate/retire 最多 5s 跨进程陈旧窗。
- **测试约定**:`asyncio_mode=auto`;conftest 注最小 env before import apihub_core;`reset_tenant_context` + `get_settings.cache_clear()` autouse;DB 测试 stub,fakeredis 替 `redis._client`。
- **lint**:ruff (E/F/I/B/UP/SIM/C4/ASYNC/S) + mypy 非严格。**每轮一个 squash-PR**;push/merge 仅在用户要求时。
- redis client 为 `decode_responses=True`(str 返回);pipeline 经 `raw_client()`。

---

## File Structure

| 文件 | 责任 | 动作 |
|---|---|---|
| `services/libs/apihub-core/src/apihub_core/l1.py` | 通用 TTL+LRU 缓存 | Create |
| `services/libs/apihub-core/src/apihub_core/config.py` | L1 Settings 字段 | Modify |
| `services/libs/apihub-core/src/apihub_core/identity.py` | opt-in L1 hook + pipeline 读 | Modify |
| `services/libs/apihub-core/src/apihub_core/auth.py` | `_verify_hmac` 用 pipeline 读 + httpx 共享 client | Modify |
| `services/services/dispatcher/src/dispatcher/resolver.py` | snapshot L1 | Modify |
| `services/services/dispatcher/src/dispatcher/main.py` | lifespan 注入 L1 | Modify |
| `services/libs/apihub-core/tests/test_l1.py` | TTLCache 单测 | Create |
| `services/libs/apihub-core/tests/test_identity_l1.py` | identity L1 + pipeline 单测 | Create |
| `services/libs/apihub-core/tests/test_hmac_verify.py` | _verify_hmac pipeline 路径回归 | Modify |
| `services/services/dispatcher/tests/test_resolver.py` | snapshot L1 单测 | Modify |

---

## Task 1: `apihub_core/l1.py` — TTLCache

**Files:**
- Create: `services/libs/apihub-core/src/apihub_core/l1.py`
- Test: `services/libs/apihub-core/tests/test_l1.py`

**Interfaces:**
- Produces: `TTLCache(maxsize=4096, ttl=5.0)` with `.get(key)->object|None` / `.set(key,value)->None` / `.invalidate(key)->None` / `.clear()->None`。

- [ ] **Step 1: Write failing test** — `services/libs/apihub-core/tests/test_l1.py`

```python
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
    c.set("c", 3)           # 满 → 淘汰最老 b
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest services/libs/apihub-core/tests/test_l1.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'apihub_core.l1'`

- [ ] **Step 3: Implement** — `services/libs/apihub-core/src/apihub_core/l1.py`

```python
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
```

- [ ] **Step 4: Run tests, verify pass**

Run: `.venv/bin/pytest services/libs/apihub-core/tests/test_l1.py -v`
Expected: PASS 5/5

- [ ] **Step 5: Commit**

```bash
git add services/libs/apihub-core/src/apihub_core/l1.py services/libs/apihub-core/tests/test_l1.py
git commit -m "R3e T1: apihub_core.l1 TTLCache（进程内 TTL+LRU）"
```

---

## Task 2: Settings — L1 字段

**Files:**
- Modify: `services/libs/apihub-core/src/apihub_core/config.py`（Settings 类内，`hmac_*` 字段后）
- Test: `services/libs/apihub-core/tests/test_config_security.py`（追加默认值断言）

**Interfaces:**
- Produces: `Settings.dispatcher_l1_enabled: bool = True` / `dispatcher_l1_ttl_seconds: float = 5.0` / `dispatcher_l1_maxsize: int = 4096`。

- [ ] **Step 1: Write failing test** — 追加到 `tests/test_config_security.py`

```python
def test_dispatcher_l1_defaults(monkeypatch):
    for k in ("DISPATCHER_L1_ENABLED", "DISPATCHER_L1_TTL_SECONDS", "DISPATCHER_L1_MAXSIZE"):
        monkeypatch.delenv(k, raising=False)
    from apihub_core.config import get_settings
    get_settings.cache_clear()
    s = get_settings()
    assert s.dispatcher_l1_enabled is True
    assert s.dispatcher_l1_ttl_seconds == 5.0
    assert s.dispatcher_l1_maxsize == 4096
    get_settings.cache_clear()
```

- [ ] **Step 2: Run → FAIL**（`AttributeError: dispatcher_l1_enabled`）

Run: `.venv/bin/pytest services/libs/apihub-core/tests/test_config_security.py::test_dispatcher_l1_defaults -v`

- [ ] **Step 3: Implement** — `config.py` Settings 类内紧随 `hmac_*` 后：

```python
    # R3e: dispatcher 进程内 L1 缓存（identity/snapshot，TTL=5s 削峰；Redis 仍为真相源）
    dispatcher_l1_enabled: bool = True
    dispatcher_l1_ttl_seconds: float = 5.0
    dispatcher_l1_maxsize: int = 4096
```

- [ ] **Step 4: Run → PASS**

Run: `.venv/bin/pytest services/libs/apihub-core/tests/test_config_security.py -v`

- [ ] **Step 5: Commit**

```bash
git add services/libs/apihub-core/src/apihub_core/config.py services/libs/apihub-core/tests/test_config_security.py
git commit -m "R3e T2: Settings dispatcher_l1_* 字段"
```

---

## Task 3: `identity.py` opt-in L1 hook + pipeline 读

**Files:**
- Modify: `services/libs/apihub-core/src/apihub_core/identity.py`
- Test: `services/libs/apihub-core/tests/test_identity_l1.py`

**Interfaces:**
- Consumes: `apihub_core.l1.TTLCache`, `apihub_core.redis.raw_client`
- Produces:
  - `identity.configure_l1(*, identity: TTLCache | None = None, secret: TTLCache | None = None) -> None`
  - `identity.read_identity` / `read_hmac_secret`：configured 则先查 L1,miss→Redis→回填
  - `identity.delete_identity` / `delete_hmac_secret`：同时 invalidate 对应 L1
  - `identity.read_identity_and_hmac_secret(api_key) -> tuple[dict | None, str | None]`：L1 优先,miss 批 pipeline Redis

- [ ] **Step 1: Write failing test** — `services/libs/apihub-core/tests/test_identity_l1.py`

```python
"""identity opt-in L1 + pipeline 读单测。"""
import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("PG_HOST", "localhost")
    monkeypatch.setenv("PG_USER", "apihub")
    monkeypatch.setenv("PG_PASSWORD", "t")
    monkeypatch.setenv("REDIS_HOST", "localhost")
    from apihub_core.config import get_settings
    get_settings.cache_clear()
    yield
    from apihub_core import identity
    identity.configure_l1(identity=None, secret=None)  # 清 L1（默认关）
    get_settings.cache_clear()


@pytest.fixture
def fake_redis(monkeypatch):
    import fakeredis.aioredis
    from apihub_core import redis as redis_mod
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_mod, "_client", fake)
    return fake


async def test_read_identity_l1_hit_skips_redis(fake_redis):
    from apihub_core import identity
    from apihub_core.l1 import TTLCache
    identity.configure_l1(identity=TTLCache(ttl=5), secret=TTLCache(ttl=5))
    identity._identity_l1.set("ak_x", {"is_active": True, "tenant_id": "t1"})
    got = await identity.read_identity("ak_x")
    assert got == {"is_active": True, "tenant_id": "t1"}
    from apihub_core.redis import raw_client
    assert await raw_client().get(identity.identity_cache_key("ak_x")) is None


async def test_read_identity_l1_miss_falls_to_redis_and_backfills(fake_redis):
    from apihub_core import identity
    from apihub_core.l1 import TTLCache
    identity.configure_l1(identity=TTLCache(ttl=5), secret=TTLCache(ttl=5))
    await identity.write_identity("ak_x", {"is_active": True, "tenant_id": "t1"}, ttl=300)
    identity._identity_l1.clear()
    got = await identity.read_identity("ak_x")
    assert got["tenant_id"] == "t1"
    assert identity._identity_l1.get("ak_x") is not None


async def test_read_identity_unconfigured_no_l1(fake_redis):
    from apihub_core import identity
    await identity.write_identity("ak_x", {"is_active": True, "tenant_id": "t1"}, ttl=300)
    got = await identity.read_identity("ak_x")
    assert got["tenant_id"] == "t1"
    assert identity._identity_l1 is None


async def test_delete_identity_invalidates_l1(fake_redis):
    from apihub_core import identity
    from apihub_core.l1 import TTLCache
    identity.configure_l1(identity=TTLCache(ttl=5), secret=TTLCache(ttl=5))
    identity._identity_l1.set("ak_x", {"is_active": True, "tenant_id": "t1"})
    await identity.delete_identity("ak_x")
    assert identity._identity_l1.get("ak_x") is None


async def test_read_identity_and_hmac_secret_both_l1_hit(fake_redis):
    from apihub_core import identity
    from apihub_core.l1 import TTLCache
    identity.configure_l1(identity=TTLCache(ttl=5), secret=TTLCache(ttl=5))
    identity._identity_l1.set("ak_x", {"is_active": True, "tenant_id": "t1"})
    identity._secret_l1.set("ak_x", "encblob")
    ident, sec = await identity.read_identity_and_hmac_secret("ak_x")
    assert ident["tenant_id"] == "t1"
    assert sec == "encblob"


async def test_read_identity_and_hmac_secret_redis_pipeline(fake_redis):
    """两 L1 miss → 一次 pipeline 取两 Redis key（unenrolled：secret None）。"""
    from apihub_core import identity
    from apihub_core.l1 import TTLCache
    identity.configure_l1(identity=TTLCache(ttl=5), secret=TTLCache(ttl=5))
    await identity.write_identity("ak_x", {"is_active": True, "tenant_id": "t1"}, ttl=300)
    ident, sec = await identity.read_identity_and_hmac_secret("ak_x")
    assert ident is not None and ident["tenant_id"] == "t1"
    assert sec is None  # 未 enrolled
    assert identity._identity_l1.get("ak_x") is not None  # 回填
```

- [ ] **Step 2: Run → FAIL**（`AttributeError: configure_l1`）

Run: `.venv/bin/pytest services/libs/apihub-core/tests/test_identity_l1.py -v`

- [ ] **Step 3: Implement** — 改 `identity.py`：

顶部 import 区加：
```python
from apihub_core.l1 import TTLCache
```

模块级（`from apihub_core import redis` 之后）加：
```python
_identity_l1: TTLCache | None = None
_secret_l1: TTLCache | None = None


def configure_l1(*, identity: TTLCache | None = None, secret: TTLCache | None = None) -> None:
    """opt-in L1（dispatcher 进程注入）。None = 关。默认全 None（不改变既有行为）。"""
    global _identity_l1, _secret_l1
    _identity_l1 = identity
    _secret_l1 = secret


async def _parse_identity(api_key: str, raw: object) -> dict[str, Any] | None:
    """Redis 原始值 → identity dict（损坏/非 dict → 清缓存返 None）。"""
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        await redis.raw_client().delete(identity_cache_key(api_key))
        return None
    if not isinstance(data, dict):
        await redis.raw_client().delete(identity_cache_key(api_key))
        return None
    return data
```

把 `read_identity` 改为 L1-aware（原 json/类型校验内联进 `_parse_identity`）：
```python
async def read_identity(api_key: str) -> dict[str, Any] | None:
    """读身份缓存。dict（含可能 {"invalid": True}）或 None（miss/损坏）。"""
    if _identity_l1 is not None:
        cached = _identity_l1.get(api_key)
        if isinstance(cached, dict):
            return cached
    raw = await redis.raw_client().get(identity_cache_key(api_key))
    parsed = await _parse_identity(api_key, raw)
    if _identity_l1 is not None and isinstance(parsed, dict):
        _identity_l1.set(api_key, parsed)
    return parsed
```

把 `read_hmac_secret` 改为 L1-aware：
```python
async def read_hmac_secret(api_key: str) -> str | None:
    """读加密 secret blob（miss/损坏返 None）。"""
    if _secret_l1 is not None:
        cached = _secret_l1.get(api_key)
        if isinstance(cached, str):
            return cached
    raw = await redis.raw_client().get(hmac_secret_cache_key(api_key))
    val = raw if isinstance(raw, str) else None
    if _secret_l1 is not None and val is not None:
        _secret_l1.set(api_key, val)
    return val
```

`delete_identity` / `delete_hmac_secret` 加 L1 逐出：
```python
async def delete_identity(api_key: str) -> None:
    if _identity_l1 is not None:
        _identity_l1.invalidate(api_key)
    await redis.raw_client().delete(identity_cache_key(api_key))


async def delete_hmac_secret(api_key: str) -> None:
    if _secret_l1 is not None:
        _secret_l1.invalidate(api_key)
    await redis.raw_client().delete(hmac_secret_cache_key(api_key))
```

新增 pipeline 读：
```python
async def read_identity_and_hmac_secret(
    api_key: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """L1 优先；任一 miss → 批 pipeline Redis（1 RTT）。投机取 secret（unenrolled → None）。"""
    ident: dict[str, Any] | None = None
    sec: str | None = None
    if _identity_l1 is not None:
        hit = _identity_l1.get(api_key)
        if isinstance(hit, dict):
            ident = hit
    if _secret_l1 is not None:
        hit = _secret_l1.get(api_key)
        if isinstance(hit, str):
            sec = hit
    need: list[tuple[str, str]] = []
    if ident is None:
        need.append(("ident", identity_cache_key(api_key)))
    if sec is None:
        need.append(("secret", hmac_secret_cache_key(api_key)))
    if not need:
        return ident, sec
    pipe = redis.raw_client().pipeline()
    for _, k in need:
        pipe.get(k)
    results = await pipe.execute()
    raw_map = {need[i][0]: results[i] for i in range(len(need))}
    if "ident" in raw_map:
        ident = await _parse_identity(api_key, raw_map["ident"])
        if _identity_l1 is not None and isinstance(ident, dict):
            _identity_l1.set(api_key, ident)
    if "secret" in raw_map:
        v = raw_map["secret"]
        sec = v if isinstance(v, str) else None
        if _secret_l1 is not None and sec is not None:
            _secret_l1.set(api_key, sec)
    return ident, sec
```

- [ ] **Step 4: Run → PASS**

Run: `.venv/bin/pytest services/libs/apihub-core/tests/test_identity_l1.py -v`
Expected: PASS 6/6

- [ ] **Step 5: 回归**

Run: `.venv/bin/pytest services/libs/apihub-core/tests/test_identity_hmac.py services/services/auth/tests/test_cache.py -v`
Expected: 不回归（L1 默认 None）。

- [ ] **Step 6: Commit**

```bash
git add services/libs/apihub-core/src/apihub_core/identity.py services/libs/apihub-core/tests/test_identity_l1.py
git commit -m "R3e T3: identity opt-in L1 + read_identity_and_hmac_secret pipeline 读"
```

---

## Task 4: `auth.py` `_verify_hmac` 用 pipeline 读 + httpx 共享 client

**Files:**
- Modify: `services/libs/apihub-core/src/apihub_core/auth.py`
- Test: `services/libs/apihub-core/tests/test_hmac_verify.py`（追加）

**Interfaces:**
- Consumes: `identity.read_identity_and_hmac_secret`
- Produces: `_verify_hmac` 经 pipeline 读；`_get_auth_httpx_client()` 共享 `httpx.AsyncClient`。

- [ ] **Step 1: Write failing test** — 追加到 `tests/test_hmac_verify.py`

```python
async def test_verify_hmac_uses_pipeline_read(fake_redis, monkeypatch):
    """_verify_hmac 经 read_identity_and_hmac_secret 一次取 identity+secret（pipeline，非两次 GET）。"""
    from apihub_core import crypto, identity
    from apihub_core.auth import _verify_hmac
    from apihub_core.config import get_settings
    from apihub_core.l1 import TTLCache
    import apihub_core.identity as ident_mod

    identity.configure_l1(identity=TTLCache(ttl=5), secret=TTLCache(ttl=5))
    await identity.write_identity("ak_enrolledkey", {
        "is_active": True, "tenant_id": "t1", "tenant_type": "internal",
        "app_id": "app1", "key_id": "key_1", "hmac_enrolled": True,
    }, ttl=300)
    await identity.write_hmac_secret("ak_enrolledkey", crypto.encrypt_secret("the_secret"), ttl=300)
    identity._identity_l1.clear()
    identity._secret_l1.clear()

    calls = {"n": 0}
    real = ident_mod.read_identity_and_hmac_secret

    async def _spy(api_key):
        calls["n"] += 1
        return await real(api_key)

    monkeypatch.setattr(ident_mod, "read_identity_and_hmac_secret", _spy)

    req = _make_request()
    ctx = await _verify_hmac(req, get_settings(), "ak_enrolledkey")
    assert ctx.tenant_id == "t1"
    assert calls["n"] == 1
    identity.configure_l1(identity=None, secret=None)
```

- [ ] **Step 2: Run → FAIL**（当前两次独立读，未走 pipeline 函数）

Run: `.venv/bin/pytest services/libs/apihub-core/tests/test_hmac_verify.py::test_verify_hmac_uses_pipeline_read -v`

- [ ] **Step 3: Implement** — 改 `auth.py`：

(a) 共享 httpx client。模块级（import 区后）加：
```python
_auth_httpx_client: httpx.AsyncClient | None = None


def _get_auth_httpx_client() -> httpx.AsyncClient:
    """进程级共享 httpx client（冷路径连接复用）。lazy 单例，遇 closed 重建。"""
    global _auth_httpx_client
    if _auth_httpx_client is None or _auth_httpx_client.is_closed:
        _auth_httpx_client = httpx.AsyncClient(timeout=5.0)
    return _auth_httpx_client
```
`_verify_via_auth_service` 内 `async with httpx.AsyncClient(timeout=5.0) as client:` 改为 `client = _get_auth_httpx_client()`（去掉 async with，后续 `client.post(...)` 不变）。
`_verify_hmac` 冷路径同理：`client = _get_auth_httpx_client()` 替代 `async with httpx.AsyncClient(timeout=5.0) as client:`。

(b) `_verify_hmac` 用 pipeline 读替换两段独立读。把开头：
```python
    cached = await identity.read_identity(api_key)
    if cached is None:
        await _verify_via_auth_service(api_key, settings)
        cached = await identity.read_identity(api_key)
    if cached is None or cached.get("invalid"):
        raise ApiError(ErrorCode.UNAUTHORIZED, "invalid api key")
    if not cached.get("is_active"):
        raise ApiError(ErrorCode.UNAUTHORIZED, "Invalid API Key")

    enrolled = cached.get("hmac_enrolled", False)
    has_sig = bool(request.headers.get("X-Signature"))
    if enrolled and not has_sig:
        raise ApiError(ErrorCode.UNAUTHORIZED, "hmac signing required for this key")
    if not enrolled and has_sig:
        raise ApiError(ErrorCode.UNAUTHORIZED, "key not enrolled for hmac")

    # I3: timestamp ±window 前置 ...
    ...
    # 取 secret：warm Redis 加密 blob ...
    secret_blob = await identity.read_hmac_secret(api_key)
    if secret_blob is not None:
        ...
```
改为（pipeline 一次取 identity+secret；timestamp/enrolled 校验顺序不变；secret_blob 已在手，删掉第二段独立 `read_hmac_secret`）：
```python
    cached, secret_blob = await identity.read_identity_and_hmac_secret(api_key)
    if cached is None:
        # C4: identity miss → 回源 auth verify 暖缓存，再 pipeline 重取
        await _verify_via_auth_service(api_key, settings)
        cached, secret_blob = await identity.read_identity_and_hmac_secret(api_key)
    if cached is None or cached.get("invalid"):
        raise ApiError(ErrorCode.UNAUTHORIZED, "invalid api key")
    if not cached.get("is_active"):
        raise ApiError(ErrorCode.UNAUTHORIZED, "Invalid API Key")

    enrolled = cached.get("hmac_enrolled", False)
    has_sig = bool(request.headers.get("X-Signature"))
    if enrolled and not has_sig:
        raise ApiError(ErrorCode.UNAUTHORIZED, "hmac signing required for this key")
    if not enrolled and has_sig:
        raise ApiError(ErrorCode.UNAUTHORIZED, "key not enrolled for hmac")

    # I3: timestamp ±window 前置（不变）...
    ...
    # secret_blob 来自 pipeline（warm）；None → 冷回源（原 if/else 逻辑不变，删去独立 read_hmac_secret 调用）
    if secret_blob is not None:
        try:
            secret = crypto_mod.decrypt_secret(secret_blob)
        except Exception:
            ...
    else:
        ... 冷回源 ...
```

- [ ] **Step 4: Run 全 hmac_verify**

Run: `.venv/bin/pytest services/libs/apihub-core/tests/test_hmac_verify.py -v`
Expected: PASS（R2e 既有 10 + 新 1）

- [ ] **Step 5: Commit**

```bash
git add services/libs/apihub-core/src/apihub_core/auth.py services/libs/apihub-core/tests/test_hmac_verify.py
git commit -m "R3e T4: _verify_hmac 用 pipeline 读 + 共享 httpx client"
```

---

## Task 5: `dispatcher/resolver.py` snapshot L1

**Files:**
- Modify: `services/services/dispatcher/src/dispatcher/resolver.py`
- Test: `services/services/dispatcher/tests/test_resolver.py`

**Interfaces:**
- Consumes: `apihub_core.l1.TTLCache`
- Produces: `resolver.configure_snapshot_l1(cache: TTLCache | None)`；`resolve_by_header` L1-aware。

> **执行前置**：先读 `resolver.py` 确认 `ApiVersionSnapshot` 字段 + retire/status 判断逻辑（本 Task Step 1 测试与 Step 3 落地按实际字段调整）。

- [ ] **Step 1: 读 resolver 现状**

Run: `sed -n '1,130p' services/services/dispatcher/src/dispatcher/resolver.py`，确认：`resolve_by_header` 签名、`cache_key`、status/retire 判断、`t_set`/`t_delete` 调用点。

- [ ] **Step 2: Write failing test** — 追加到 `tests/test_resolver.py`（用 monkeypatch spy `redis.t_get` 计 L1 命中为 0）

```python
async def test_snapshot_l1_hit_skips_redis(monkeypatch):
    """resolve_by_header L1 命中 → 不读 Redis。"""
    from dispatcher import resolver
    from apihub_core.l1 import TTLCache

    resolver.configure_snapshot_l1(TTLCache(maxsize=8, ttl=5))
    # 直接置 L1：用 _from_json 还原一个合法 snapshot 再 asdict（字段按 resolver 实际）
    import dataclasses
    snap = resolver._from_json({
        "api_id": "a", "version": 1, "backend_url": "http://b", "backend_type": "http",
        "method": "GET", "path": "/x", "visibility": "private",
    })  # 字段以 resolver.ApiVersionSnapshot 实际为准，缺则补默认
    resolver._snapshot_l1.set("snapshot:v1", dataclasses.asdict(snap))

    t_get_calls = {"n": 0}
    from apihub_core import redis as redis_mod
    orig = redis_mod.t_get

    async def _spy_t_get(key):
        t_get_calls["n"] += 1
        return await orig(key)

    monkeypatch.setattr(redis_mod, "t_get", _spy_t_get)
    out = await resolver.resolve_by_header("v1")
    assert out.api_id == "a"
    assert t_get_calls["n"] == 0  # L1 命中，未读 Redis
    resolver.configure_snapshot_l1(None)
```

- [ ] **Step 3: Run → FAIL**（`AttributeError: configure_snapshot_l1`）

Run: `.venv/bin/pytest services/services/dispatcher/tests/test_resolver.py::test_snapshot_l1_hit_skips_redis -v`

- [ ] **Step 4: Implement** — `resolver.py`：

import 区加 `from apihub_core.l1 import TTLCache`。模块级加：
```python
_snapshot_l1: TTLCache | None = None


def configure_snapshot_l1(cache: TTLCache | None) -> None:
    global _snapshot_l1
    _snapshot_l1 = cache
```

`resolve_by_header` 开头（算出 `cache_key` 后）插 L1 查询；Redis 命中回填 L1；retire/invalidate 处也逐出 L1；DB 回源 `t_set` 后也回填 L1。骨架（按实际 status 判断调整）：
```python
async def resolve_by_header(version_id: str) -> ApiVersionSnapshot:
    cache_key = f"snapshot:{version_id}"
    if _snapshot_l1 is not None:
        hit = _snapshot_l1.get(cache_key)
        if isinstance(hit, dict):
            return _from_json(hit)
    cached = await redis.t_get(cache_key)
    if cached:
        data = json.loads(cached)
        snap = _from_json(data)
        if getattr(snap, "status", None) in ("deprecated", "retired"):  # 以实际字段为准
            await redis.t_delete(cache_key)
            if _snapshot_l1 is not None:
                _snapshot_l1.invalidate(cache_key)
        else:
            if _snapshot_l1 is not None:
                _snapshot_l1.set(cache_key, data)
            return snap
    # ... 既有 DB 查 + await redis.t_set(cache_key, json.dumps(asdict(snapshot)), ex=300) ...
    # 在 t_set 后补：if _snapshot_l1 is not None: _snapshot_l1.set(cache_key, dataclasses.asdict(snapshot))
```

- [ ] **Step 5: Run → PASS + 回归**

Run: `.venv/bin/pytest services/services/dispatcher/tests/test_resolver.py -v`

- [ ] **Step 6: Commit**

```bash
git add services/services/dispatcher/src/dispatcher/resolver.py services/services/dispatcher/tests/test_resolver.py
git commit -m "R3e T5: resolver snapshot L1"
```

---

## Task 6: `dispatcher/main.py` lifespan 注入 L1

**Files:**
- Modify: `services/services/dispatcher/src/dispatcher/main.py`

**Interfaces:**
- Consumes: `identity.configure_l1`、`resolver.configure_snapshot_l1`、`apihub_core.l1.TTLCache`、`Settings.dispatcher_l1_*`

- [ ] **Step 1: 读 main.py lifespan 结构**

Run: `grep -n "lifespan\|AsyncClient\|extra_lifespan\|get_settings\|create_app" services/services/dispatcher/src/dispatcher/main.py`

- [ ] **Step 2: Implement** — 在 lifespan/extra_lifespan 的 Redis 就绪后、yield 前注入；yield 后（shutdown）清理：

```python
from apihub_core import identity as identity_mod
from apihub_core.l1 import TTLCache
from dispatcher import resolver as resolver_mod

# yield 前（redis 就绪后）：
if settings.dispatcher_l1_enabled:
    _ttl = settings.dispatcher_l1_ttl_seconds
    _max = settings.dispatcher_l1_maxsize
    identity_mod.configure_l1(
        identity=TTLCache(maxsize=_max, ttl=_ttl),
        secret=TTLCache(maxsize=_max, ttl=_ttl),
    )
    resolver_mod.configure_snapshot_l1(TTLCache(maxsize=_max, ttl=_ttl))

# yield 后（shutdown）：
identity_mod.configure_l1(identity=None, secret=None)
resolver_mod.configure_snapshot_l1(None)
```
> 注：`settings = get_settings()` 已在 main.py 取（或在此处取）。若 main.py 用 `create_app(extra_lifespan=...)`，把上述放 extra_lifespan 的 yield 前后。

- [ ] **Step 3: import 验证**

Run: `cd services/services/dispatcher && PG_HOST=x PG_USER=x PG_PASSWORD=x REDIS_HOST=x .venv/bin/python -c "import sys; sys.path.insert(0,'src'); from dispatcher.main import app; print('import ok')"`
Expected: import ok。

- [ ] **Step 4: Commit**

```bash
git add services/services/dispatcher/src/dispatcher/main.py
git commit -m "R3e T6: dispatcher lifespan 注入 L1（identity + snapshot）"
```

---

## Task 7: 观测埋点 + 全量回归 + review

- [ ] **Step 1: 埋点** — `identity.read_identity`/`read_hmac_secret` L1 命中/miss 时 `log.debug("identity_l1_hit"/"identity_l1_miss", key=...)`（用 `apihub_core.logging.get_logger`）。仅 debug 级，无 assert。可选 resolver 同理。

- [ ] **Step 2: 全量回归（逐 suite，避免 basename 冲突）**

```
.venv/bin/pytest services/libs/apihub-core/tests/ -q
.venv/bin/pytest services/services/dispatcher/tests/ -q
.venv/bin/pytest services/services/auth/tests/ -q
```
Expected: 全绿（apihub-core 147 + T1/T3 新增；dispatcher 61 + T5；auth 77），无新失败。

- [ ] **Step 3: lint**

Run: `ruff check services/libs/apihub-core/src services/services/dispatcher/src services/libs/apihub-core/tests services/services/dispatcher/tests && ~/.local/bin/mypy services/libs/apihub-core/src/apihub_core/identity.py services/libs/apihub-core/src/apihub_core/auth.py services/libs/apihub-core/src/apihub_core/l1.py services/services/dispatcher/src/dispatcher/resolver.py`
Expected: ruff clean；mypy 无新 error（pre-existing 5 不计）。

- [ ] **Step 4: opus whole-branch review**（per 用户工作风格：spec→plan→handoff，one squash-PR per round）。处理 Critical/Important；handoff 用户 push/merge。

---

## Self-Review

**Spec coverage:** §3.1 L1 → T1+T2+T3+T5+T6 ✓；§3.2 pipelining(HMAC) → T3+T4，bearer defer(spec 记) ✓；§3.3 httpx 池化 → T4 ✓；§6 错误 → T1(LRU)+T3(corrupt→miss)+T4(503) ✓；§7 正确性 → L1 存数据非决策 + 既有 .get ✓；§8 配置 → T2 ✓；§9 测试 → 各 Task TDD ✓；§10 部署 → T6 enable 开关 ✓。

**Placeholder scan:** T5 resolver 字段以实际为准（Step 1 读源码确认 + Step 2 测试/Step 4 落地据此调整）——已显式标注执行前置；余无 TBD。

**Type consistency:** `TTLCache.get/set/invalidate/clear`、`configure_l1(identity=,secret=)`、`configure_snapshot_l1`、`read_identity_and_hmac_secret -> (dict|None,str|None)` 跨 Task 一致。
