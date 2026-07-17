# R1d — APISIX 鉴权闭环 + dispatcher 信任入口（修 good-key 503）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 APISIX 成为真正的鉴权+限流层（consumer 生命周期 + 发布路由带 key-auth/limit-count），并让 dispatcher 经 APISIX 入口时**不再每请求 HTTP 回源 auth**（消除 good-key 冷启动 503）。

**Architecture:**
- **APISIX consumer 由 auth 管（随 key 生命周期）**：`create_api_key` 建 consumer（username=`key_id`，per-key 而非 per-app，支持多 key）+ 预热 Redis 身份缓存；`revoke_api_key` 删 consumer + 清缓存。路由由 api-registry 管（随 publish），R1c 已做。
- **发布路由带 key-auth + 限流**：`publish_route` payload 加 `key-auth`（edge 校验，无效 key→401 不到 dispatcher）、条件 `limit-count`（从 `api_version.rate_limit` 映射）、`proxy-rewrite` 注入 `X-Ingress-Auth=<secret>`（可信入口证明）。
- **dispatcher 信任入口（503 修复）**：`apihub_core.auth.authenticate_request` 加快速路径——若 `X-Ingress-Auth == ingress_shared_secret`，跳过 HTTP auth 调用，**本地读 auth 的 Redis 身份缓存**（`ak:{sha256}`，dispatcher 已连同一 Redis）回填 TenantContext；缓存 miss 回落原 HTTP 流程（回落会预热缓存）。503 真因是 dispatcher→auth 的 `httpx.RequestError`（连接拒绝/超时），改读集群内 Redis（亚毫秒）即消除。
- APISIX 插件集无 `serverless-pre-function`，无法逐 consumer 注入 header —— 故身份走 Redis 缓存而非 APISIX header 注入（验证可行、无 Lua 猜测）。

**Tech Stack:** Python 3.11 / FastAPI / asyncpg / redis(asyncio) / httpx / APISIX 3.x（key-auth + limit-count + proxy-rewrite）/ pytest-asyncio / Kustomize + kind。

**Spec:** `docs/superpowers/specs/2026-07-17-r1d-apisix-auth-design.md`（本 plan 在其基础上：① 修正 consumer 改 per-key；② 明确 503 修复机制为「信任入口 + Redis 身份缓存」，因 APISIX 插件集不支持 header 注入；③ 把 dispatcher 信任入口纳入本轮范围——用户已选 Option B）。

## Global Constraints

- **不破坏 RLS**：dispatcher 信任路径仍必须回填 `TenantContext`（从 Redis 读 `tenant_id`/`app_id`），`db_session()` 的 `SET LOCAL app.tenant_id` 与 visibility 不变。
- **可信入口安全不变量**：`X-Ingress-Auth` 信任要求 **dispatcher 仅经 APISIX 可达**（ClusterIP，无外部 ingress）。否则 header 可被直连 dispatcher 的调用方伪造，绕过鉴权。k8s 里 dispatcher 是 ClusterIP Service，APISIX 是唯一 ingress——满足；docs 任务需写明此不变量。
- **consumer 一对一 per-key**（username=`key_id`，非 spec 原写的 `app_id`）：APISIX key-auth consumer 只能持一个 `key`，per-app 会让同 app 第 2 个 key 覆盖第 1 个。consumer_name 对下游不透明（信任路径用 Redis，不读 consumer_name），故 per-key 无副作用，且 revoke 时删该 key 的 consumer 干净。
- **best-effort + 审计**：consumer/缓存的 APISIX/Redis 写是非事务副作用。create_key 时若 upsert_consumer 失败，**不回滚 key 创建**（key 仍可用——dispatcher 回落 HTTP auth），仅记审计日志（后续可对账）。revoke 同理 best-effort。
- **限流映射**：`api_version.rate_limit` = `{count, window_seconds}`（jsonb，`scripts/init-db/01-schema.sql:133`，已存在）→ APISIX `limit-count` = `{count, time_window=window_seconds, key="consumer_name", policy="local", rejected_code=429}`。prod 用 `policy="redis-cluster"` 为 follow-up（spec 风险点）。
- **测试约定**：`asyncio_mode=auto`；apihub-core 测试通过 `monkeypatch.setenv` + `get_settings.cache_clear()` 注入最小 env（PG/REDIS 必填）。httpx 用 `monkeypatch.setattr(httpx, "AsyncClient", FakeClient)` stub（见现有 `test_apisix_client.py`）。
- **APISIX admin key（kind）**：`edd1c9f034335f136f87ad84b625c8f1`（chart 默认兜底，`scripts/kind/apisix-setup.sh:205`）；prod 走 Sealed Secret。
- **commit 粒度**：每个 Task 末尾一次 commit；本轮全部合为一个 squash-PR（用户约定 one-PR-per-round）。

---

## File Structure

**新建：**
- `services/libs/apihub-core/src/apihub_core/apisix_client.py` — 从 api-registry 迁来；`publish_route`（+key-auth/limit/ingress header）、`upsert_consumer`、`delete_consumer`、`_admin_request`、`_normalize_path`、`retire_route`。
- `services/libs/apihub-core/src/apihub_core/identity.py` — Redis 身份缓存契约（`identity_cache_key` / `read_identity` / `write_identity` / `delete_identity`），auth 与 dispatcher 共享单一真相源。
- `services/libs/apihub-core/tests/test_apisix_client.py` — 从 api-registry 迁来 + 新增 consumer/limit 用例。
- `services/libs/apihub-core/tests/test_identity.py` — identity 缓存读写 + middleware 信任路径用例。

**修改：**
- `services/libs/apihub-core/src/apihub_core/auth.py` — `authenticate_request` 加「可信入口」快速路径。
- `services/libs/apihub-core/src/apihub_core/config.py` — 新增 `ingress_shared_secret`（`apisix_admin_url`/`apisix_admin_key`/`dispatcher_upstream` 已在）。
- `services/libs/apihub-core/src/apihub_core/__init__.py` — 导出 `identity` 符号。
- `services/services/api-registry/src/api_registry/routes.py` — publish handler 传 `rate_limit=row["rate_limit"]`；import 改 `from apihub_core import apisix_client`。
- `services/services/auth/src/auth/routes.py` — `create_key` 后 upsert_consumer + 预热缓存；`revoke_key` 后 delete_consumer。
- `services/services/auth/src/auth/cache.py` — `cache_key`/`get_cached`/`cache_positive`/`cache_negative`/`invalidate` 改委托 `apihub_core.identity`（单一真相源）。
- `deploy/k8s/services/api-registry/configmap.yaml` — Secret 补 `APISIX_ADMIN_KEY`、`INGRESS_SHARED_SECRET`；ConfigMap 补 `INGRESS_SHARED_SECRET`（非敏）。
- `deploy/k8s/services/auth/configmap.yaml` — ConfigMap 补 `APISIX_ADMIN_URL`、`INGRESS_SHARED_SECRET`；Secret 补 `APISIX_ADMIN_KEY`、`INGRESS_SHARED_SECRET`。
- `scripts/kind/apisix-setup.sh` — smoke 路由注入 `X-Ingress-Auth`，与 `INGRESS_SHARED_SECRET` 一致。
- `docs/aggregate-ownership.md` — consumer/auth 归属 + 可信入口不变量。

**删除：**
- `services/services/api-registry/src/api_registry/apisix_client.py`（迁到 core）。
- `services/services/api-registry/tests/test_apisix_client.py`（迁到 core）。

---

## Task 1: 迁移 apisix_client 到 apihub_core（纯 move，行为不变）

把 api-registry 的 `apisix_client.py` 原样迁到 `apihub_core`，改 import 源，迁移测试。此 Task **不改 publish_route 行为**（key-auth/limit 在 Task 3 加），降低 review 风险。

**Files:**
- Create: `services/libs/apihub-core/src/apihub_core/apisix_client.py`
- Delete: `services/services/api-registry/src/api_registry/apisix_client.py`
- Delete+recreate: `services/services/api-registry/tests/test_apisix_client.py` → `services/libs/apihub-core/tests/test_apisix_client.py`
- Modify: `services/services/api-registry/src/api_registry/routes.py:13`（import 源）

**Interfaces:**
- Consumes: `apihub_core.config.get_settings`（`apisix_admin_url`/`apisix_admin_key`/`dispatcher_upstream`）、`apihub_core.errors`
- Produces: `apihub_core.apisix_client.publish_route(*, version_id, method, path, base_path) -> None`、`retire_route(version_id)`、`_normalize_path`、`_admin_request`（签名与原 api-registry 模块完全一致）

- [ ] **Step 1: 创建 core 版 apisix_client.py（原样复制）**

把 `services/services/api-registry/src/api_registry/apisix_client.py` 的全部内容（含 docstring、`_normalize_path`/`_admin_request`/`publish_route`/`retire_route`）原样写到 `services/libs/apihub-core/src/apihub_core/apisix_client.py`。import 已是 `from apihub_core.config import get_settings` / `from apihub_core.errors import ApiError, ErrorCode`，无需改。

- [ ] **Step 2: 迁移测试到 core**

创建 `services/libs/apihub-core/tests/test_apisix_client.py`，内容 = 原 `services/services/api-registry/tests/test_apisix_client.py`，仅改 import：`from api_registry import apisix_client` → `from apihub_core import apisix_client`（3 处：`test_publish_route_puts_admin_route`、`test_publish_route_normalizes_path_vars`、`test_publish_route_non_2xx_raises_502`）。其余断言（uri/methods/upstream/proxy-rewrite/502）不变。

- [ ] **Step 3: 写 env 注入 fixture（core conftest 不注入 PG/REDIS）**

`services/libs/apihub-core/tests/test_apisix_client.py` 顶部 fixture（合并原 `_settings`）：

```python
import httpx
import pytest
from apihub_core.errors import ApiError


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    from apihub_core.config import get_settings

    # core conftest 不注入必填 env，这里自给（Settings 构造需要 PG/REDIS）
    monkeypatch.setenv("PG_HOST", "localhost")
    monkeypatch.setenv("PG_USER", "apihub")
    monkeypatch.setenv("PG_PASSWORD", "test")
    monkeypatch.setenv("REDIS_HOST", "localhost")
    monkeypatch.setenv("APISIX_ADMIN_URL", "http://apisix-admin.apihub-ingress:9180")
    monkeypatch.setenv("APISIX_ADMIN_KEY", "edd1c9f034335f136f87ad84b625c8f1")
    monkeypatch.setenv("DISPATCHER_UPSTREAM", "dispatcher.apihub-system:8001")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
```

- [ ] **Step 4: 删除 api-registry 的旧模块与旧测试**

```bash
git rm services/services/api-registry/src/api_registry/apisix_client.py
git rm services/services/api-registry/tests/test_apisix_client.py
```

- [ ] **Step 5: 改 api-registry routes.py import 源**

`services/services/api-registry/src/api_registry/routes.py:13`：

```python
# 旧
from api_registry import apisix_client
# 新
from apihub_core import apisix_client
```

（`routes.py` 内调用处 `apisix_client.publish_route(...)` 不变。）

- [ ] **Step 6: 跑测试验证迁移成功**

Run:
```bash
pytest services/libs/apihub-core/tests/test_apisix_client.py -v
pytest services/services/api-registry/tests/ -v
```
Expected: core 的 3 个 apisix_client 用例 PASS；api-registry 全部测试 PASS（无 import error）。

- [ ] **Step 7: Commit**

```bash
git add services/libs/apihub-core/src/apihub_core/apisix_client.py \
        services/libs/apihub-core/tests/test_apisix_client.py \
        services/services/api-registry/src/api_registry/routes.py \
        services/services/api-registry/src/api_registry/apisix_client.py \
        services/services/api-registry/tests/test_apisix_client.py
git commit -m "refactor(r1d): migrate apisix_client api-registry -> apihub_core (no behavior change)"
```

---

## Task 2: apisix_client 加 consumer 生命周期（upsert/delete_consumer）

**Files:**
- Modify: `services/libs/apihub-core/src/apihub_core/apisix_client.py`
- Modify: `services/libs/apihub-core/tests/test_apisix_client.py`

**Interfaces:**
- Produces:
  - `upsert_consumer(*, key_id: str, key: str) -> None` — `PUT {admin}/consumers/{key_id}` body `{"username": key_id, "plugins": {"key-auth": {"key": key, "header": "X-API-Key"}}}`
  - `delete_consumer(key_id: str) -> None` — `DELETE {admin}/consumers/{key_id}`，404 静默（不当错）

- [ ] **Step 1: 写 upsert_consumer 失败测试**

追加到 `test_apisix_client.py`：

```python
async def test_upsert_consumer_puts_admin_consumer(monkeypatch):
    captured = {}

    class _FakeResp:
        status_code = 201

        def json(self):
            return {"ok": True}

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def request(self, method, url, **kw):
            captured["method"] = method
            captured["url"] = url
            captured["json"] = kw.get("json")
            return _FakeResp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    from apihub_core import apisix_client

    await apisix_client.upsert_consumer(key_id="key_abc", key="ak_secret")

    assert captured["method"] == "PUT"
    assert (
        captured["url"]
        == "http://apisix-admin.apihub-ingress:9180/apisix/admin/consumers/key_abc"
    )
    assert captured["json"] == {
        "username": "key_abc",
        "plugins": {"key-auth": {"key": "ak_secret", "header": "X-API-Key"}},
    }
```

- [ ] **Step 2: 跑测试验证失败**

Run: `pytest services/libs/apihub-core/tests/test_apisix_client.py::test_upsert_consumer_puts_admin_consumer -v`
Expected: FAIL `AttributeError: module 'apihub_core.apisix_client' has no attribute 'upsert_consumer'`

- [ ] **Step 3: 实现 upsert_consumer + delete_consumer**

在 `services/libs/apihub-core/src/apihub_core/apisix_client.py` 的 `retire_route` 之前插入：

```python
async def upsert_consumer(*, key_id: str, key: str) -> None:
    """upsert APISIX consumer（username=key_id，per-key）—— 随 APIKey 生命周期。

    consumer 持 key-auth 凭证（key=明文，header=X-API-Key），APISIX 在网关层秒级校验。
    per-key（非 per-app）：APISIX key-auth consumer 只能持一个 key，per-app 会让同 app
    第 2 个 key 覆盖第 1 个。consumer_name 对下游不透明（信任路径走 Redis，不读它）。
    """
    settings = get_settings()
    if not settings.apisix_admin_url:
        raise ApiError(ErrorCode.INTERNAL, "APISIX_ADMIN_URL not configured", http_status=500)
    body = {
        "username": key_id,
        "plugins": {"key-auth": {"key": key, "header": "X-API-Key"}},
    }
    await _admin_request(
        "PUT",
        f"{settings.apisix_admin_url}/apisix/admin/consumers/{key_id}",
        json=body,
    )


async def delete_consumer(key_id: str) -> None:
    """删 APISIX consumer（随 key 吊销）。不存在静默（404 不当错）。"""
    settings = get_settings()
    if not settings.apisix_admin_url:
        return  # 未配 APISIX（dev 无 APISIX 时 no-op）
    try:
        await _admin_request(
            "DELETE",
            f"{settings.apisix_admin_url}/apisix/admin/consumers/{key_id}",
        )
    except ApiError as e:
        # 404 = consumer 本就不存在（revoke 幂等），静默；其余（5xx/网络）抛出
        if "failed: 404" not in str(e):
            raise
```

- [ ] **Step 4: 跑测试验证通过**

Run: `pytest services/libs/apihub-core/tests/test_apisix_client.py::test_upsert_consumer_puts_admin_consumer -v`
Expected: PASS

- [ ] **Step 5: 写 delete_consumer 两个用例（正常 + 404 静默）**

```python
async def test_delete_consumer_deletes(monkeypatch):
    captured = {}

    class _FakeResp:
        status_code = 200

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def request(self, method, url, **kw):
            captured["method"] = method
            captured["url"] = url
            return _FakeResp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    from apihub_core import apisix_client

    await apisix_client.delete_consumer("key_abc")  # 不抛即通过
    assert captured["method"] == "DELETE"
    assert captured["url"].endswith("/apisix/admin/consumers/key_abc")


async def test_delete_consumer_404_is_silent(monkeypatch):
    class _FakeResp:
        status_code = 404
        text = '{"error":"not found"}'

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def request(self, method, url, **kw):
            return _FakeResp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    from apihub_core import apisix_client

    await apisix_client.delete_consumer("key_abc")  # 404 不抛
```

- [ ] **Step 6: 跑全部 apisix_client 测试**

Run: `pytest services/libs/apihub-core/tests/test_apisix_client.py -v`
Expected: 全 PASS（含原 3 + upsert + delete×2）

- [ ] **Step 7: Commit**

```bash
git add services/libs/apihub-core/src/apihub_core/apisix_client.py \
        services/libs/apihub-core/tests/test_apisix_client.py
git commit -m "feat(r1d): apisix_client consumer lifecycle (upsert/delete, per-key)"
```

---

## Task 3: publish_route 加 key-auth + 限流 + 可信入口 header

**Files:**
- Modify: `services/libs/apihub-core/src/apihub_core/apisix_client.py`（`publish_route`）
- Modify: `services/libs/apihub-core/src/apihub_core/config.py`（`ingress_shared_secret`）
- Modify: `services/libs/apihub-core/tests/test_apisix_client.py`
- Modify: `services/services/api-registry/src/api_registry/routes.py:153`（传 rate_limit）

**Interfaces:**
- Produces: `publish_route(*, version_id: str, method: str, path: str, base_path: str, rate_limit: dict | None = None) -> None`
  - plugins 恒含 `key-auth`（header=X-API-Key）+ `proxy-rewrite`（X-API-Version-Id；若 `ingress_shared_secret` 配置则加 `X-Ingress-Auth`）
  - 若 `rate_limit`（`{count, window_seconds}`）有 `count`：加 `limit-count`

- [ ] **Step 1: config.py 加 ingress_shared_secret**

`services/libs/apihub-core/src/apihub_core/config.py`，在 `apisix_admin_key` 行（约 L75）后加：

```python
    apisix_admin_key: str | None = None
    # 可信入口共享密钥：APISIX proxy-rewrite 注入 X-API-Auth=<本值>，dispatcher 信任路径据此
    # 跳过 HTTP auth 回源。安全前提：dispatcher 仅经 APISIX 可达（ClusterIP，无外部 ingress）。
    ingress_shared_secret: str | None = None
    dispatcher_upstream: str = "dispatcher.apihub-system:80"
```

（注：header 名是 `X-Ingress-Auth`；上面注释里的 `X-API-Auth` 是笔误，以代码 `X-Ingress-Auth` 为准。）

- [ ] **Step 2: 写 publish_route 含 key-auth + ingress header 的失败测试**

追加到 `test_apisix_client.py`：

```python
async def test_publish_route_includes_keyauth_and_ingress_header(monkeypatch):
    from apihub_core.config import get_settings

    monkeypatch.setenv("INGRESS_SHARED_SECRET", "s3cr3t-ingress")
    get_settings.cache_clear()
    captured = {}

    class _FakeResp:
        status_code = 201

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def request(self, method, url, **kw):
            captured["json"] = kw.get("json")
            return _FakeResp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    from apihub_core import apisix_client

    await apisix_client.publish_route(
        version_id="ver_abc", method="GET", path="/u", base_path="/v1"
    )
    plugins = captured["json"]["plugins"]
    assert plugins["key-auth"] == {"header": "X-API-Key"}
    assert plugins["proxy-rewrite"]["headers"]["set"] == {
        "X-API-Version-Id": "ver_abc",
        "X-Ingress-Auth": "s3cr3t-ingress",
    }
    assert "limit-count" not in plugins  # 无 rate_limit
    get_settings.cache_clear()


async def test_publish_route_includes_limit_count_when_rate_limit_set(monkeypatch):
    captured = {}

    class _FakeResp:
        status_code = 201

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def request(self, method, url, **kw):
            captured["json"] = kw.get("json")
            return _FakeResp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    from apihub_core import apisix_client

    await apisix_client.publish_route(
        version_id="ver_abc",
        method="GET",
        path="/u",
        base_path="/v1",
        rate_limit={"count": 10, "window_seconds": 60},
    )
    lc = captured["json"]["plugins"]["limit-count"]
    assert lc == {
        "count": 10,
        "time_window": 60,
        "key": "consumer_name",
        "policy": "local",
        "rejected_code": 429,
    }
```

- [ ] **Step 3: 跑测试验证失败**

Run: `pytest services/libs/apihub-core/tests/test_apisix_client.py::test_publish_route_includes_keyauth_and_ingress_header -v`
Expected: FAIL（`publish_route() got an unexpected keyword argument 'rate_limit'` 或 plugins 无 key-auth）

- [ ] **Step 4: 实现 publish_route 新签名 + plugins**

替换 `apisix_client.py` 的 `publish_route` 为：

```python
async def publish_route(
    *,
    version_id: str,
    method: str,
    path: str,
    base_path: str,
    rate_limit: dict | None = None,
) -> None:
    """upsert 一条 APISIX 路由（id=version_id）→ dispatcher + key-auth + 限流 + 注入 header。

    - key-auth：edge 校验 X-API-Key（无效→401，不到 dispatcher）。
    - limit-count：仅当 rate_limit（{count, window_seconds}）有 count 时加。
    - proxy-rewrite：注入 X-API-Version-Id（dispatcher 强制）；若 ingress_shared_secret 配置，
      同步注入 X-Ingress-Auth（dispatcher 据此走信任路径，跳过 HTTP auth 回源）。
    """
    settings = get_settings()
    if not settings.apisix_admin_url:
        raise ApiError(ErrorCode.INTERNAL, "APISIX_ADMIN_URL not configured", http_status=500)

    uri = (base_path.rstrip("/") + _normalize_path(path)) if base_path else _normalize_path(path)
    set_headers = {"X-API-Version-Id": version_id}
    if settings.ingress_shared_secret:
        set_headers["X-Ingress-Auth"] = settings.ingress_shared_secret

    plugins: dict = {
        "key-auth": {"header": "X-API-Key"},
        "proxy-rewrite": {
            "regex_uri": ["^/(.*)$", "/dispatch/$1"],
            "headers": {"set": set_headers},
        },
    }
    if rate_limit and rate_limit.get("count"):
        plugins["limit-count"] = {
            "count": int(rate_limit["count"]),
            "time_window": int(rate_limit.get("window_seconds", 60)),
            "key": "consumer_name",
            "policy": "local",
            "rejected_code": 429,
        }

    body = {
        "uri": uri,
        "methods": [method.upper()],
        "upstream": {"type": "roundrobin", "nodes": {settings.dispatcher_upstream: 1}},
        "plugins": plugins,
    }
    await _admin_request(
        "PUT",
        f"{settings.apisix_admin_url}/apisix/admin/routes/{version_id}",
        json=body,
    )
```

- [ ] **Step 5: 跑测试验证通过**

Run: `pytest services/libs/apihub-core/tests/test_apisix_client.py -v`
Expected: 全 PASS。注意：原 `test_publish_route_puts_admin_route` 断言 `body["plugins"]["proxy-rewrite"]["headers"]["set"] == {"X-API-Version-Id": "ver_abc"}`——该用例的 `_settings` fixture 未设 `INGRESS_SHARED_SECRET`（None），set_headers 只含 X-API-Version-Id，**断言仍成立**，无需改。

- [ ] **Step 6: api-registry publish handler 传 rate_limit**

`services/services/api-registry/src/api_registry/routes.py`，把 `await apisix_client.publish_route(...)` 调用（约 L153）加一行：

```python
            await apisix_client.publish_route(
                version_id=version_id,
                method=row["method"],
                path=row["path"],
                base_path=row["base_path"],
                rate_limit=row["rate_limit"],
            )
```

（`row` 来自 `SELECT v.*, a.base_path`，`v.*` 含 `rate_limit` jsonb 列 → asyncpg jsonb codec 返回 dict 或 None。）

- [ ] **Step 7: 跑 api-registry 测试确认无回归**

Run: `pytest services/services/api-registry/tests/ -v`
Expected: 全 PASS（publish 路径现有测试不断言 APISIX payload 形状，rate_limit 透传不影响）。

- [ ] **Step 8: Commit**

```bash
git add services/libs/apihub-core/src/apihub_core/apisix_client.py \
        services/libs/apihub-core/src/apihub_core/config.py \
        services/libs/apihub-core/tests/test_apisix_client.py \
        services/services/api-registry/src/api_registry/routes.py
git commit -m "feat(r1d): publish_route key-auth + limit-count + ingress trust header"
```

---

## Task 4: apihub_core.identity + dispatcher 信任入口快速路径（503 修复核心）

新增 `identity` 模块统一 Redis 身份缓存契约；`authenticate_request` 加信任路径。middleware 不动。

**Files:**
- Create: `services/libs/apihub-core/src/apihub_core/identity.py`
- Create: `services/libs/apihub-core/tests/test_identity.py`
- Modify: `services/libs/apihub-core/src/apihub_core/auth.py`（`authenticate_request`）
- Modify: `services/libs/apihub-core/src/apihub_core/__init__.py`（导出 identity 符号）

**Interfaces:**
- Produces:
  - `identity.identity_cache_key(api_key: str) -> str` → `"ak:" + sha256(api_key).hexdigest()`
  - `await identity.read_identity(api_key) -> dict | None`（返回 VerifyResponse 字段 dict；负缓存返回 `{"invalid": True}`；miss 返回 None）
  - `await identity.write_identity(api_key, data: dict, ttl: int) -> None`
  - `await identity.delete_identity(api_key) -> None`
- `auth.authenticate_request` 新增分支：header `X-Ingress-Auth == settings.ingress_shared_secret`（且非空）→ 读 `read_identity(api_key)`；命中且非 invalid → 建 ctx 返回；invalid → 401；miss → 回落原 HTTP 流程。

- [ ] **Step 1: 写 identity 模块失败测试**

创建 `services/libs/apihub-core/tests/test_identity.py`：

```python
"""identity 缓存契约测试 —— 与 auth.cache 共享单一真相源。"""

import json

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("PG_HOST", "localhost")
    monkeypatch.setenv("PG_USER", "apihub")
    monkeypatch.setenv("PG_PASSWORD", "test")
    monkeypatch.setenv("REDIS_HOST", "localhost")
    from apihub_core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_identity_cache_key_is_ak_sha256():
    import hashlib

    from apihub_core.identity import identity_cache_key

    api_key = "ak_abc123"
    assert identity_cache_key(api_key) == "ak:" + hashlib.sha256(api_key.encode()).hexdigest()


async def test_write_then_read_identity(monkeypatch):
    stored = {}

    class _FakeRedis:
        async def setex(self, key, ttl, val):
            stored[key] = (ttl, val)

        async def get(self, key):
            return stored.get(key, (None, None))[1]

        async def delete(self, key):
            stored.pop(key, None)

    from apihub_core import identity, redis as redis_mod

    monkeypatch.setattr(redis_mod, "raw_client", lambda: _FakeRedis())

    await identity.write_identity("ak_x", {"tenant_id": "t1", "app_id": "a1"}, ttl=300)
    got = await identity.read_identity("ak_x")
    assert got == {"tenant_id": "t1", "app_id": "a1"}


async def test_read_identity_miss_returns_none(monkeypatch):
    class _FakeRedis:
        async def get(self, key):
            return None

    from apihub_core import identity, redis as redis_mod

    monkeypatch.setattr(redis_mod, "raw_client", lambda: _FakeRedis())
    assert await identity.read_identity("ak_x") is None


async def test_delete_identity(monkeypatch):
    class _FakeRedis:
        async def delete(self, key):
            pass

    from apihub_core import identity, redis as redis_mod

    monkeypatch.setattr(redis_mod, "raw_client", lambda: _FakeRedis())
    await identity.delete_identity("ak_x")  # 不抛即通过
```

- [ ] **Step 2: 跑测试验证失败**

Run: `pytest services/libs/apihub-core/tests/test_identity.py -v`
Expected: FAIL（`ModuleNotFoundError: apihub_core.identity`）

- [ ] **Step 3: 实现 identity.py**

创建 `services/libs/apihub-core/src/apihub_core/identity.py`：

```python
"""Redis 身份缓存契约 —— auth 写、dispatcher 读（信任路径），单一真相源。

key: ak:{sha256(api_key_plaintext)}   （与 auth.apikey.cache_key 一致）
value: JSON dict = VerifyResponse 字段（is_active/tenant_id/tenant_type/app_id/
       is_platform_admin/scopes/expires_at），或 {"invalid": True}（负缓存）。
用 redis.raw_client()（无租户前缀，因 key 本身即身份证明）。
"""

import hashlib
import json
from typing import Any

from apihub_core import redis


def identity_cache_key(api_key: str) -> str:
    """ak:{sha256(api_key)}。"""
    return "ak:" + hashlib.sha256(api_key.encode("utf-8")).hexdigest()


async def read_identity(api_key: str) -> dict[str, Any] | None:
    """读身份缓存。dict（含可能 {"invalid": True}）或 None（miss/损坏）。"""
    raw = await redis.raw_client().get(identity_cache_key(api_key))
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        await redis.raw_client().delete(identity_cache_key(api_key))
        return None


async def write_identity(api_key: str, data: dict[str, Any], ttl: int) -> None:
    await redis.raw_client().setex(identity_cache_key(api_key), ttl, json.dumps(data))


async def delete_identity(api_key: str) -> None:
    await redis.raw_client().delete(identity_cache_key(api_key))
```

- [ ] **Step 4: 跑 identity 测试验证通过**

Run: `pytest services/libs/apihub-core/tests/test_identity.py -v`
Expected: 全 PASS

- [ ] **Step 5: 写 authenticate_request 信任路径失败测试**

追加到 `test_identity.py`：

```python
async def test_authenticate_request_trust_path_skips_http(monkeypatch):
    """X-Ingress-Auth 匹配 + Redis 命中 → 跳过 HTTP auth，直接建 ctx。"""
    from apihub_core.config import get_settings

    monkeypatch.setenv("INGRESS_SHARED_SECRET", "s3cr3t")
    get_settings.cache_clear()

    cached = {
        "is_active": True,
        "tenant_id": "t1",
        "tenant_type": "internal",
        "app_id": "app1",
        "is_platform_admin": False,
    }

    class _FakeRedis:
        async def get(self, key):
            return json.dumps(cached)

    from apihub_core import redis as redis_mod
    from apihub_core.auth import authenticate_request

    monkeypatch.setattr(redis_mod, "raw_client", lambda: _FakeRedis())

    class _FakeRequest:
        def __init__(self):
            self.headers = {"X-API-Key": "ak_real", "X-Ingress-Auth": "s3cr3t"}

    http_called = []

    class _NoHttp:
        def __init__(self, *a, **kw):
            http_called.append(True)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *a, **kw):
            raise AssertionError("trust path must not call auth HTTP")

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _NoHttp)

    ctx = await authenticate_request(_FakeRequest(), get_settings(), "ak_real")
    assert ctx.tenant_id == "t1"
    assert ctx.app_id == "app1"
    assert http_called == []  # 没建 http client
    get_settings.cache_clear()


async def test_authenticate_request_trust_path_miss_falls_back_to_http(monkeypatch):
    """X-Ingress-Auth 匹配但 Redis miss → 回落 HTTP auth（预热缓存）。"""
    from apihub_core.config import get_settings

    monkeypatch.setenv("INGRESS_SHARED_SECRET", "s3cr3t")
    get_settings.cache_clear()

    class _FakeRedis:
        async def get(self, key):
            return None  # miss

    from apihub_core import redis as redis_mod
    from apihub_core.auth import authenticate_request

    monkeypatch.setattr(redis_mod, "raw_client", lambda: _FakeRedis())

    class _FakeResp:
        status_code = 200

        def json(self):
            return {
                "is_active": True,
                "tenant_id": "t2",
                "tenant_type": "internal",
                "app_id": "app2",
                "is_platform_admin": False,
            }

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *a, **kw):
            return _FakeResp()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    class _FakeRequest:
        def __init__(self):
            self.headers = {"X-API-Key": "ak_real", "X-Ingress-Auth": "s3cr3t"}

    ctx = await authenticate_request(_FakeRequest(), get_settings(), "ak_real")
    assert ctx.tenant_id == "t2"  # 来自 HTTP 回落
    get_settings.cache_clear()
```

- [ ] **Step 6: 跑测试验证失败**

Run: `pytest services/libs/apihub-core/tests/test_identity.py::test_authenticate_request_trust_path_skips_http -v`
Expected: FAIL（信任路径未实现 → 当前走 HTTP，`AssertionError: trust path must not call auth HTTP`）

- [ ] **Step 7: 在 authenticate_request 加信任路径**

`services/libs/apihub-core/src/apihub_core/auth.py`，在 `if not api_key: raise ...`（L28-29）之后、JWT 分流（L31）之前插入：

```python
    # 可信入口快速路径（R1d）：经 APISIX 入口的请求带 X-Ingress-Auth=<ingress_shared_secret>。
    # APISIX key-auth 已校验 key，本地读 auth 的 Redis 身份缓存回填 ctx，跳过 HTTP auth 回源
    # （消除 dispatcher→auth 冷启动 503）。安全前提：dispatcher 仅经 APISIX 可达（见 docs）。
    if (
        settings.ingress_shared_secret
        and request.headers.get("X-Ingress-Auth") == settings.ingress_shared_secret
    ):
        from apihub_core import identity

        cached = await identity.read_identity(api_key)
        if cached is not None:
            if cached.get("invalid") or not cached.get("is_active"):
                raise ApiError(ErrorCode.UNAUTHORIZED, "Invalid API Key")
            ctx = TenantContext(
                tenant_id=cached["tenant_id"],
                tenant_type=cached.get("tenant_type", "internal"),
                app_id=cached.get("app_id"),
                is_platform_admin=cached.get("is_platform_admin", False),
            )
            set_tenant_context(ctx)
            return ctx
        # miss → 回落下方 HTTP auth（会预热缓存）
```

- [ ] **Step 8: 跑全部 identity 测试验证通过**

Run: `pytest services/libs/apihub-core/tests/test_identity.py -v`
Expected: 全 PASS（含 trust-path skip + miss-fallback）

- [ ] **Step 9: 跑 auth 现有测试确认无回归**

Run: `pytest services/libs/apihub-core/tests/test_auth.py -v`
Expected: 全 PASS（未设 INGRESS_SHARED_SECRET 时信任路径不激活，行为不变）

- [ ] **Step 10: __init__.py 导出 identity**

`services/libs/apihub-core/src/apihub_core/__init__.py`，在 `from apihub_core.errors import ...` 块之后加：

```python
from apihub_core.identity import (  # noqa: E402
    delete_identity,
    identity_cache_key,
    read_identity,
    write_identity,
)
```

Run: `python -c "from apihub_core import identity, read_identity; print('ok')"`
Expected: `ok`

- [ ] **Step 11: Commit**

```bash
git add services/libs/apihub-core/src/apihub_core/identity.py \
        services/libs/apihub-core/src/apihub_core/auth.py \
        services/libs/apihub-core/src/apihub_core/__init__.py \
        services/libs/apihub-core/tests/test_identity.py
git commit -m "feat(r1d): trusted-ingress fast path in authenticate_request (fix good-key 503)"
```

---

## Task 5: auth 接 consumer 生命周期 + 预热缓存 + cache.py 委托 identity

create_key 建 consumer + 预热 Redis 身份缓存；revoke 删 consumer + 清缓存；auth.cache 改委托 identity（单一真相源）。

**Files:**
- Modify: `services/services/auth/src/auth/routes.py`（`create_key` ~L134、`revoke_key` ~L173）
- Modify: `services/services/auth/src/auth/cache.py`（委托 identity）
- Create: `services/services/auth/tests/test_key_lifecycle_apisix.py`

**Interfaces:**
- Consumes: `apihub_core.apisix_client.upsert_consumer/delete_consumer`、`apihub_core.identity.write_identity/delete_identity`、`auth.apikey.POSITIVE_CACHE_TTL`
- `auth.cache.cache_key/get_cached/cache_positive/cache_negative/invalidate` 签名不变（仅实现委托）

- [ ] **Step 1: 写 create_key/revoke_key 副作用失败测试**

创建 `services/services/auth/tests/test_key_lifecycle_apisix.py`：

```python
"""create_key/revoke_key 的 APISIX consumer + Redis 缓存副作用测试。"""


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    # auth tests/conftest.py 已注入最小 env；补 APISIX
    monkeypatch.setenv("APISIX_ADMIN_URL", "http://apisix-admin.apihub-ingress:9180")
    monkeypatch.setenv("APISIX_ADMIN_KEY", "edd1c9f034335f136f87ad84b625c8f1")
    from apihub_core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_create_key_upserts_consumer_and_warms_cache(monkeypatch):
    import pytest  # noqa: F401

    calls = {"consumer": None, "cache_warm": None}

    async def _upsert(*, key_id, key):
        calls["consumer"] = (key_id, key)

    async def _write(api_key, data, ttl):
        calls["cache_warm"] = (api_key, data, ttl)

    from apihub_core import apisix_client, identity
    from auth import routes as routes_mod

    monkeypatch.setattr(apisix_client, "upsert_consumer", _upsert)
    monkeypatch.setattr(identity, "write_identity", _write)

    async def _fake_create(**kw):
        return {
            "id": kw["key_id"],
            "app_id": kw["app_id"],
            "name": kw["name"],
            "scopes": kw["scopes"],
            "display_prefix": kw["display_prefix"],
            "expires_at": kw["expires_at"],
            "created_at": "2026-07-17T00:00:00+00:00",
        }

    monkeypatch.setattr(routes_mod, "create_api_key", _fake_create)

    from apihub_core import auth as auth_mod, kafka as k_mod
    from apihub_core.tenant import TenantContext, set_tenant_context

    async def _noop_emit(*a, **kw):
        pass

    monkeypatch.setattr(k_mod, "emit", _noop_emit)

    ctx = TenantContext(tenant_id="t1", tenant_type="internal", is_platform_admin=True)

    async def _noop_auth(request, settings, api_key):  # noqa: ARG001
        set_tenant_context(ctx)
        return ctx

    monkeypatch.setattr(auth_mod, "authenticate_request", _noop_auth)

    import httpx
    from auth.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        resp = await c.post(
            "/v1/apps/app_1/api-keys",
            headers={"X-API-Key": "ak_x"},
            json={"name": "n", "scopes": []},
        )
    assert resp.status_code == 200, resp.text
    plaintext = resp.json()["api_key"]
    assert calls["consumer"][0] == resp.json()["id"]  # key_id
    assert calls["consumer"][1] == plaintext
    assert calls["cache_warm"][0] == plaintext
    assert calls["cache_warm"][1]["app_id"] == "app_1"


async def test_revoke_key_deletes_consumer_and_cache(monkeypatch):
    calls = {"consumer_del": None, "cache_del": None}

    async def _delete_consumer(key_id):
        calls["consumer_del"] = key_id

    async def _delete_identity(api_key):
        calls["cache_del"] = api_key

    from apihub_core import apisix_client, identity
    from auth import routes as routes_mod

    monkeypatch.setattr(apisix_client, "delete_consumer", _delete_consumer)
    monkeypatch.setattr(identity, "delete_identity", _delete_identity)

    async def _fake_revoke(key_id):
        return {"id": key_id, "app_id": "app_1", "key_hash": "h"}

    monkeypatch.setattr(routes_mod, "revoke_api_key", _fake_revoke)

    from apihub_core import auth as auth_mod, kafka as k_mod
    from apihub_core.tenant import TenantContext, set_tenant_context

    async def _noop_emit(*a, **kw):
        pass

    monkeypatch.setattr(k_mod, "emit", _noop_emit)

    ctx = TenantContext(tenant_id="t1", tenant_type="internal", is_platform_admin=True)

    async def _noop_auth(request, settings, api_key):  # noqa: ARG001
        set_tenant_context(ctx)
        return ctx

    monkeypatch.setattr(auth_mod, "authenticate_request", _noop_auth)

    import httpx
    from auth.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        resp = await c.delete("/v1/api-keys/key_1", headers={"X-API-Key": "ak_x"})
    assert resp.status_code == 200, resp.text
    assert calls["consumer_del"] == "key_1"
    # cache.invalidate 用 key_hash → delete_identity 被调（具体入参是 hash）
    assert calls["cache_del"] is not None
```

（注：本文件顶部需 `import pytest`，已在 `_env` 内补 `import pytest` 占位——实际放文件顶部。）

- [ ] **Step 2: 跑测试验证失败**

Run: `pytest services/services/auth/tests/test_key_lifecycle_apisix.py -v`
Expected: FAIL（`upsert_consumer`/`delete_consumer` 未被调，`calls["consumer"] is None`）

- [ ] **Step 3: 改 create_key 接 consumer + 预热**

`services/services/auth/src/auth/routes.py`，在 `create_key` 内 `record = await create_api_key(...)` 之后、`log.info("apikey_created"...)` 之前插入（`plaintext`/`key_id`/`ctx`/`app_id`/`payload` 均在作用域）：

```python
        # R1d：随 key 生命周期同步 APISIX consumer（edge 校验）+ 预热 Redis 身份缓存
        # （dispatcher 信任路径命中即不回源 auth）。best-effort：失败仅记日志，不回滚 key
        # （key 仍可用——dispatcher 回落 HTTP auth）。
        try:
            from apihub_core import apisix_client, identity
            from auth.apikey import POSITIVE_CACHE_TTL

            await apisix_client.upsert_consumer(key_id=key_id, key=plaintext)
            await identity.write_identity(
                plaintext,
                {
                    "is_active": True,
                    "tenant_id": ctx.tenant_id,
                    "tenant_type": ctx.tenant_type,
                    "app_id": app_id,
                    "is_platform_admin": ctx.is_platform_admin,
                    "scopes": payload.scopes,
                    "expires_at": payload.expires_at.isoformat()
                    if payload.expires_at
                    else None,
                },
                ttl=POSITIVE_CACHE_TTL,
            )
        except Exception:  # noqa: BLE001
            log.warning("apisix_consumer_upsert_failed", key_id=key_id, app_id=app_id)
```

- [ ] **Step 4: 改 revoke_key 接 delete_consumer**

`revoke_key` 内 `await invalidate(revoked["key_hash"])` 之后（约 L183）插入：

```python
        # R1d：删 APISIX consumer（best-effort）
        try:
            from apihub_core import apisix_client

            await apisix_client.delete_consumer(key_id)
        except Exception:  # noqa: BLE001
            log.warning("apisix_consumer_delete_failed", key_id=key_id)
```

（`key_id` 是 revoke_key 路径参数；缓存已由 `invalidate` 清。）

- [ ] **Step 5: cache.py 委托 identity（单一真相源）**

把 `services/services/auth/src/auth/cache.py` 改为：

```python
"""Redis 缓存读写 —— 委托 apihub_core.identity（单一真相源）。

缓存策略：
  - 正缓存（合法 key）: 5 分钟
  - 负缓存（非法 key）: 1 分钟（防爆破）
  - 吊销时主动 DEL

key/value 契约见 apihub_core.identity。
"""

from typing import Any

from apihub_core import identity

from auth.apikey import (
    NEGATIVE_CACHE_TTL,
    POSITIVE_CACHE_TTL,
    cache_key,
)


async def cache_positive(api_key_plaintext: str, data: dict[str, Any]) -> None:
    await identity.write_identity(api_key_plaintext, data, ttl=POSITIVE_CACHE_TTL)


async def cache_negative(api_key_plaintext: str) -> None:
    await identity.write_identity(
        api_key_plaintext, {"invalid": True}, ttl=NEGATIVE_CACHE_TTL
    )


async def get_cached(api_key_plaintext: str) -> dict[str, Any] | None:
    return await identity.read_identity(api_key_plaintext)


async def invalidate(api_key_plaintext_or_hash: str) -> None:
    """吊销时主动清缓存。入参可为明文或 hash（cache_key 两者兼容）。"""
    # revoke_key 传 key_hash（非明文），identity.delete_identity 用明文算 key —— 故此处
    # 仍走 auth.apikey.cache_key（兼容明文/-hash），直接操作 raw_client，保持原行为。
    from apihub_core import redis

    await redis.raw_client().delete(cache_key(api_key_plaintext_or_hash))


async def warmup(api_key_plaintext: str, data: dict[str, Any]) -> None:
    await cache_positive(api_key_plaintext, data)
```

- [ ] **Step 6: 跑新测试 + auth 现有测试**

Run:
```bash
pytest services/services/auth/tests/test_key_lifecycle_apisix.py -v
pytest services/services/auth/tests/ -v
```
Expected: 新 2 用例 PASS；auth 现有用例（含 test_apikey.py 的 cache_key 断言、test_cache.py）全 PASS。

- [ ] **Step 7: Commit**

```bash
git add services/services/auth/src/auth/routes.py \
        services/services/auth/src/auth/cache.py \
        services/services/auth/tests/test_key_lifecycle_apisix.py
git commit -m "feat(r1d): auth key lifecycle drives APISIX consumer + identity cache warm"
```

---

## Task 6: k8s 配置补齐（APISIX_ADMIN_KEY + INGRESS_SHARED_SECRET）+ 跨 ns 连通验证

**Files:**
- Modify: `deploy/k8s/services/api-registry/configmap.yaml`（ConfigMap + Secret）
- Modify: `deploy/k8s/services/auth/configmap.yaml`（ConfigMap + Secret）
- Modify: `scripts/kind/apisix-setup.sh`（smoke 路由注入 X-Ingress-Auth）

**Interfaces:** 无代码接口；纯配置。

- [ ] **Step 1: api-registry configmap 补 INGRESS_SHARED_SECRET（ConfigMap 非敏）+ Secret 补两 key**

`deploy/k8s/services/api-registry/configmap.yaml`，ConfigMap `data:` 在 `DISPATCHER_UPSTREAM` 后加：

```yaml
  # 可信入口共享密钥（非敏值可入 ConfigMap；prod 用强随机值，最好走 Secret）
  INGRESS_SHARED_SECRET: ingress-shared-dev
```

同文件 Secret 注释段补（示意；prod 走 Sealed Secret）：

```yaml
# stringData:
#   PG_PASSWORD: ...
#   APISIX_ADMIN_KEY: <from sealed secret>   # kind 用 edd1c9f034335f136f87ad84b625c8f1
#   INGRESS_SHARED_SECRET: <strong random>   # prod 强随机；须与 APISIX proxy-rewrite 注入值一致
```

- [ ] **Step 2: auth configmap 补 APISIX_ADMIN_URL + INGRESS_SHARED_SECRET + Secret 补两 key**

`deploy/k8s/services/auth/configmap.yaml`，ConfigMap `data:` 末尾加：

```yaml
  # APISIX admin（auth 管 consumer 生命周期）+ 可信入口
  APISIX_ADMIN_URL: http://apisix-admin.apihub-ingress:9180
  INGRESS_SHARED_SECRET: ingress-shared-dev
```

Secret 注释段补：

```yaml
# stringData:
#   PG_PASSWORD: ...
#   APISIX_ADMIN_KEY: <from sealed secret>      # kind 用 edd1c9f034335f136f87ad84b625c8f1
#   INGRESS_SHARED_SECRET: <strong random>      # 与 api-registry 一致
```

- [ ] **Step 3: apisix-setup.sh smoke 路由注入 X-Ingress-Auth**

`scripts/kind/apisix-setup.sh`，§6b 的 `dispatcher` 路由 PUT payload（约 L248-250）。先在文件顶部（其他默认值附近）加：

```bash
[ -z "${INGRESS_SHARED_SECRET:-}" ] && INGRESS_SHARED_SECRET="ingress-shared-dev"
```

并把 §6b 路由 payload 改为：

```bash
curl -s "${ADMIN}/routes/dispatcher" -H "X-API-KEY: ${ADMIN_KEY}" -X PUT \
  -d '{"uri":"/dispatch/*","upstream":{"type":"roundrobin","nodes":{"dispatcher.apihub-system:80":1}},"plugins":{"key-auth":{"header":"X-API-Key"},"proxy-rewrite":{"headers":{"set":{"X-API-Version-Id":"ver_smoke_sync_v1","X-Ingress-Auth":"'"${INGRESS_SHARED_SECRET}"'"}}}}}' \
  -o /dev/null -w "  route dispatcher PUT -> %{http_code}\n"
```

（§7 good-key smoke 经此路由会带 X-Ingress-Auth → dispatcher 信任路径生效。）

- [ ] **Step 4: 跨 ns 连通验证步骤（写进本 Task 说明，e2e 在 Task 8 执行）**

无 NetworkPolicy manifest 存在（`grep -rln NetworkPolicy deploy/` 仅注释命中），kind kindnetd 默认 allow-all 跨 ns。验证命令（Task 8 跑）：

```bash
# 从 api-registry pod curl apisix-admin（跨 ns apihub-system -> apihub-ingress）
kubectl -n apihub-system exec deploy/api-registry -- \
  curl -s -o /dev/null -w "%{http_code}\n" \
  -H "X-API-KEY: edd1c9f034335f136f87ad84b625c8f1" \
  http://apisix-admin.apihub-ingress:9180/apisix/admin/consumers
# 期望 200。若连不上（000/超时）→ 补 NetworkPolicy allow apihub-system -> apihub-ingress:9180（见 Step 5）。
```

- [ ] **Step 5: 备选 NetworkPolicy（仅当 Step 4 失败时 apply）**

仅在 Task 8 验证连不上时创建 `deploy/k8s/base/apigw/allow-apihub-to-apisix-admin.yaml`：

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-apihub-to-apisix-admin
  namespace: apihub-ingress
spec:
  podSelector: {}
  policyTypes: [Ingress]
  ingress:
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: apihub-system
      ports:
        - protocol: TCP
          port: 9180
```

（默认不 apply；Task 8 决定。）

- [ ] **Step 6: yaml 校验**

Run: `python scripts/validate-schema.py 2>/dev/null || true`（无 schema 校验则跳）；人工核对 YAML 缩进。

- [ ] **Step 7: Commit**

```bash
git add deploy/k8s/services/api-registry/configmap.yaml \
        deploy/k8s/services/auth/configmap.yaml \
        scripts/kind/apisix-setup.sh
git commit -m "chore(r1d): k8s APISIX_ADMIN_KEY + INGRESS_SHARED_SECRET for auth & api-registry"
```

---

## Task 7: 文档 — 归属 + 可信入口不变量

**Files:**
- Modify: `docs/aggregate-ownership.md`

- [ ] **Step 1: 读现有 §9 护栏段落**

Run: `grep -n "APISIX\|9-A\|9-B\|consumer\|路由\|归属" docs/aggregate-ownership.md | head`

- [ ] **Step 2: 追加 consumer/路由归属 + 可信入口不变量**

在 `docs/aggregate-ownership.md` 末尾（§9 之后）追加：

```markdown
## R1d：APISIX consumer / 路由 / 可信入口归属

- **APISIX consumer** 由 **auth** 管（随 APIKey 生命周期）：`create_key` 建 consumer
  （username=`key_id`，per-key）+ 预热 Redis 身份缓存；`revoke_key` 删 consumer + 清缓存。
  auth 是 app/key 聚合的唯一拥有者（§9-B），故 consumer 归它。
- **APISIX route** 由 **api-registry** 管（随 publish）：`publish_route` 下发（R1c），
  带 key-auth + 条件 limit-count + 注入 X-API-Version-Id / X-Ingress-Auth。
- **可信入口不变量（安全）**：dispatcher 的 `authenticate_request` 信任 `X-Ingress-Auth`
  header 跳过 HTTP auth 回源（修 good-key 503）。此信任成立的前提是 **dispatcher 仅经
  APISIX 可达（ClusterIP，无外部 ingress）**——APISIX proxy-rewrite 用 `set` 覆写调用方
  提供的同名 header，故只有经 APISIX 的请求才带可信值。若 dispatcher 被外部直连暴露，
  该 header 可伪造、绕过鉴权。任何新增 dispatcher 暴露面（额外 ingress/NodePort）前必须
  重审此不变量。
```

- [ ] **Step 3: Commit**

```bash
git add docs/aggregate-ownership.md
git commit -m "docs(r1d): APISIX consumer/route ownership + trusted-ingress invariant"
```

---

## Task 8: kind e2e 验证（真实 APISIX 数据面）

前置：kind 集群在跑、APISIX 已部署（`scripts/kind/apisix-setup.sh` 已执行或重跑）、`make dev-up` 栈（PG/Redis）就绪。本 Task **不写代码**，只跑验证并把结果记入 PR 描述。

- [ ] **Step 1: 重建镜像并部署 auth/api-registry（带 R1d 改动）**

```bash
make docker-build SERVICE=auth
make docker-build SERVICE=api-registry
kubectl -n apihub-system rollout restart deploy/auth deploy/api-registry
kubectl -n apihub-system rollout status deploy/auth deploy/api-registry
```

- [ ] **Step 2: 重跑 apisix-setup.sh（注入 X-Ingress-Auth 的 smoke 路由）**

```bash
unset ALL_PROXY HTTPS_PROXY HTTP_PROXY; export NO_PROXY=127.0.0.1
bash scripts/kind/apisix-setup.sh
```
Expected: §6 consumer/route PUT 返 200/201；§7 "no key → 401"、"good-key → 200" 通过。

- [ ] **Step 3: 跨 ns 连通（Task 6 Step 4 命令）**

```bash
kubectl -n apihub-system exec deploy/api-registry -- \
  curl -s -o /dev/null -w "%{http_code}\n" \
  -H "X-API-KEY: edd1c9f034335f136f87ad84b625c8f1" \
  http://apisix-admin.apihub-ingress:9180/apisix/admin/consumers
```
Expected: `200`。非 200 → apply Task 6 Step 5 NetworkPolicy 后重试。

- [ ] **Step 4: no-key → 401 在 APISIX（不到 dispatcher）**

经 APISIX gateway（NodePort 30080 或 port-forward）调一个已 publish 的路由，不带 X-API-Key：

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:30080/<published-uri>
```
Expected: `401`（APISIX key-auth 拒），且 dispatcher 日志**无**该请求记录。

- [ ] **Step 5: good-key → 200（信任路径，不回源 auth）**

```bash
curl -s -o /dev/null -w "%{http_code}\n" -H "X-API-Key: ${DEMO_KEY}" http://127.0.0.1:30080/<published-uri>
```
Expected: `200`。验证信任路径：auth 日志**无** `/v1/apikey/verify` 调用（dispatcher 走 Redis 缓存）。

- [ ] **Step 6: revoke key → good-key 变 401**

经 auth 吊销该 key（或直接 DELETE consumer），再用同 key 调：

```bash
curl -s -X DELETE -H "X-API-KEY: edd1c9f034335f136f87ad84b625c8f1" \
  http://127.0.0.1:<admin-pf>/apisix/admin/consumers/<key_id>
curl -s -o /dev/null -w "%{http_code}\n" -H "X-API-Key: ${DEMO_KEY}" http://127.0.0.1:30080/<published-uri>
```
Expected: `401`（consumer 删了，APISIX key-auth 拒）。APISIX key-auth 内部缓存有秒级窗口，可短暂重试确认转 401。

- [ ] **Step 7: 限流 → 429（若 publish 的 API 设了 rate_limit）**

对一个 rate_limit={count:5,window:60} 的 API 连打 10 次：

```bash
for i in $(seq 1 10); do
  curl -s -o /dev/null -w "%{http_code}\n" -H "X-API-Key: ${DEMO_KEY}" http://127.0.0.1:30080/<rate-limited-uri>
done
```
Expected: 前 5 个 200，之后 `429`（APISIX limit-count，不到 dispatcher）。

- [ ] **Step 8: good-key 503 回归验证（核心）**

冷启 dispatcher 后立即连打 good-key 20 次：

```bash
for i in $(seq 1 20); do
  curl -s -o /dev/null -w "%{http_code}\n" -H "X-API-Key: ${DEMO_KEY}" http://127.0.0.1:30080/<published-uri>
done | sort | uniq -c
```
Expected: 全 `200`，**无 503**（R1c 的 good-key 冷启 503 抖动已消除——信任路径走 Redis 不回源 auth）。若出现 503 → 检查 `X-Ingress-Auth` 是否注入、Redis 身份缓存是否预热（auth create_key 副作用）。

- [ ] **Step 9: 全量回归（防 R1d 改 shared middleware 波及其他服务）**

```bash
make test
```
Expected: services/ + apihub_core 全测试 PASS（信任路径仅在 header 匹配时激活，未配 INGRESS_SHARED_SECRET 的服务/测试行为不变）。

- [ ] **Step 10: 更新进度记忆 + 开 PR**

更新 `~/.claude/.../memory/apihub-fix-program-progress.md`（R1d 已合/PR 号）；`git push` 后开 squash-PR（用户约定 merge 仅在 ask 时）。

---

## Self-Review 结论

- **Spec 覆盖**：① apisix_client 迁移+consumer（Task 1-2）✓；② publish key-auth/limit（Task 3）✓；③ auth consumer 生命周期（Task 5）✓；④ k8s APISIX_ADMIN_KEY + 跨 ns（Task 6）✓；⑤ docs（Task 7）✓；用户追加的 503 修复（信任入口 + Redis 身份，Task 4-5）✓；e2e 含 spec 全部验证点（Task 8）✓。
- **对 spec 的两处偏离（已记录）**：(1) consumer 改 per-key（username=key_id）——修正 spec per-app 在多 key 下的覆盖 bug；(2) 503 修复机制改「信任入口 + Redis 身份缓存」——因 APISIX 插件集无 serverless-pre-function，无法逐 consumer 注入 header。二者均写入 Global Constraints / Architecture 供 review。
- **类型一致**：`upsert_consumer(*, key_id, key)`（Task 2 定义）↔ Task 5 调用一致；`identity.write_identity(api_key, data, ttl)`（Task 4）↔ Task 5 调用一致；`publish_route(..., rate_limit=None)`（Task 3）↔ routes.py 传 `rate_limit=row["rate_limit"]` 一致。
- **无占位符**：所有 step 含可执行代码/命令/期望输出。
- **风险已落地**：跨 ns 网络（Task 6 Step 4-5 验证 + 备选 NP）、consumer 一致性 best-effort（Task 5 Step 3 try/except + 审计）、可信入口安全前提（Task 7 文档不变量）。
