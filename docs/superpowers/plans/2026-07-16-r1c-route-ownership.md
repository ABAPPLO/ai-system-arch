# R1c — 路由归属（APISIX 动态路由 + dispatcher 退纯转发）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 闭合 §3.1（api-registry publish/retire 下发 APISIX）+ §9-A（dispatcher 退纯转发，删 path re-resolve）；顺带修 deprecated 走 header 路由 404 + retire 无 410 的既有缺陷。达成「发布即可调、deprecated 仍可调、下线即 410」。

**Architecture:** APISIX 是 prod 唯一路由层。api-registry publish 经 Admin API upsert 路由（proxy-rewrite 注入 `X-API-Version-Id` + 改写 path 到 `/dispatch/`）；dispatcher 仅按 header 解析（`resolve_by_header`），删 `resolve_by_path`，按版本状态返 410。retire 不动 APISIX（dispatcher 兜底 410），零 helm 改动。

**Tech Stack:** Python 3.11 / FastAPI / asyncpg / httpx / APISIX Admin API / pytest(asyncio_mode=auto)。

**Spec:** `docs/superpowers/specs/2026-07-16-r1c-route-ownership-design.md`（APISIX 路由 body、归一化、设计判断的权威来源——本计划不重复，需要时引用）。

## Global Constraints

- **APISIX 路由 body / proxy-rewrite / 归一化规则**：见 spec ①（`{var}`→`:var`、`regex_uri ["^/(.*)$","/dispatch/$1"]`、`X-API-Version-Id` header set、upstream=dispatcher）。本计划代码引用 spec 的 payload。
- **dispatcher 退纯转发**：`/dispatch` 强制 `X-API-Version-Id`（无→`INVALID_PARAMS` 400）；删 `resolve_by_path`。
- **生命周期映射**：`resolve_by_header` 放开 `published`+`deprecated` 可路由；`retired`→`API_RETIRED`(410)；其余未发布→`API_NOT_PUBLISHED`(404)。
- **配置**：`apisix_admin_url`/`apisix_admin_key` 已在 `apihub_core.config.Settings`（`config.py:70-71`，`str|None=None`）；新增 `dispatcher_upstream`（默认 `dispatcher.apihub-system:8001`）。
- **测试约定**：async；`httpx.ASGITransport(app)`；monkeypatch repo/httpx；DB 触达处仿各服务既有测试（api-registry 仿 `test_lifecycle.py`，dispatcher 仿 `async_client` fixture）。
- **提交节奏**：每 Task 末尾 commit；一轮一个 squash-PR；push/merge 仅在用户要求时。
- **APISIX 在 kind-apihub Running**（`apisix-76c7dcd8b-rp5z4`）；e2e 走 kind（非 `make dev-up`）。

---

## Task 1: api-registry `apisix_client.py` + `dispatcher_upstream` 配置 + 单测

**Files:**
- Create: `services/services/api-registry/src/api_registry/apisix_client.py`
- Modify: `services/libs/apihub-core/src/apihub_core/config.py`（加 `dispatcher_upstream`）
- Test: `services/services/api-registry/tests/test_apisix_client.py`

**Interfaces:**
- Produces: `publish_route(*, version_id, method, path, base_path) -> None`、`retire_route(version_id) -> None`（R1c 内 no-op，仅占位）、`_admin_request(method, url, **kw)`。

- [ ] **Step 1: 写失败测试 `test_apisix_client.py`**

```python
"""apisix_client 单测 —— stub httpx，断言 Admin API 请求形状。"""

import httpx
import pytest
from apihub_core.errors import ApiError


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    from apihub_core.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("APISIX_ADMIN_URL", "http://apisix-admin.apihub-ingress:9180")
    monkeypatch.setenv("APISIX_ADMIN_KEY", "edd1c9f034335f136f87ad84b625c8f1")
    monkeypatch.setenv("DISPATCHER_UPSTREAM", "dispatcher.apihub-system:8001")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_publish_route_puts_admin_route(monkeypatch):
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
            captured["headers"] = kw.get("headers")
            captured["json"] = kw.get("json")
            return _FakeResp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    from api_registry import apisix_client

    await apisix_client.publish_route(
        version_id="ver_abc",
        method="GET",
        path="/users/{user_id}",
        base_path="/v1",
    )

    assert captured["method"] == "PUT"
    assert (
        captured["url"]
        == "http://apisix-admin.apihub-ingress:9180/apisix/admin/routes/ver_abc"
    )
    assert captured["headers"]["X-API-KEY"] == "edd1c9f034335f136f87ad84b625c8f1"
    body = captured["json"]
    assert body["uri"] == "/v1/users/:user_id"  # base_path + path，{var}→:var
    assert body["methods"] == ["GET"]
    assert body["upstream"]["nodes"] == {"dispatcher.apihub-system:8001": 1}
    assert body["plugins"]["proxy-rewrite"]["headers"]["set"] == [
        "X-API-Version-Id: ver_abc"
    ]
    assert body["plugins"]["proxy-rewrite"]["regex_uri"] == ["^/(.*)$", "/dispatch/$1"]


async def test_publish_route_normalizes_path_vars(monkeypatch):
    """{var} → :var（APISIX radixtree 段匹配）。"""
    from api_registry import apisix_client

    assert apisix_client._normalize_path("/v1/users/{user_id}/orders/{order_id}") == (
        "/v1/users/:user_id/orders/:order_id"
    )


async def test_publish_route_non_2xx_raises_502(monkeypatch):
    class _FakeResp:
        status_code = 401

        def json(self):
            return {"error": "bad key"}

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
    from api_registry import apisix_client

    with pytest.raises(ApiError) as ei:
        await apisix_client.publish_route(
            version_id="ver_x", method="GET", path="/x", base_path="/v1"
        )
    assert ei.value.http_status == 502
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest services/services/api-registry/tests/test_apisix_client.py -v`
Expected: FAIL（`apisix_client` 不存在）。

- [ ] **Step 3: 加 `dispatcher_upstream` 配置**

`services/libs/apihub-core/src/apihub_core/config.py` 在 `apisix_admin_key` 行（L71）后加：

```python
    dispatcher_upstream: str = "dispatcher.apihub-system:8001"
```

- [ ] **Step 4: 实现 `apisix_client.py`**

```python
"""APISIX Admin API 客户端 —— publish 时 upsert 路由。

路由策略：每 published api_version 一条 APISIX route（id=version_id），
upstream 指向 dispatcher，proxy-rewrite 注入 X-API-Version-Id + 把 path 重写成 /dispatch/...。
"""

import httpx
from apihub_core.config import get_settings
from apihub_core.errors import ApiError, ErrorCode


def _normalize_path(path: str) -> str:
    """{var} → APISIX :var 段匹配。"""
    out = []
    for seg in path.strip("/").split("/"):
        if seg.startswith("{") and seg.endswith("}"):
            out.append(":" + seg[1:-1])
        else:
            out.append(seg)
    return "/" + "/".join(out)


async def _admin_request(method: str, url: str, **kw) -> httpx.Response:
    settings = get_settings()
    headers = kw.pop("headers", {})
    headers["X-API-KEY"] = settings.apisix_admin_key or ""
    async with httpx.AsyncClient(timeout=3.0) as c:
        try:
            resp = await c.request(method, url, headers=headers, **kw)
        except httpx.RequestError as e:
            raise ApiError(
                ErrorCode.INTERNAL,
                f"apisix admin unreachable: {type(e).__name__}: {e!r}",
                http_status=502,
            ) from e
    if resp.status_code < 200 or resp.status_code >= 300:
        raise ApiError(
            ErrorCode.INTERNAL,
            f"apisix admin {method} {url} failed: {resp.status_code} {resp.text[:200]}",
            http_status=502,
        )
    return resp


async def publish_route(*, version_id: str, method: str, path: str, base_path: str) -> None:
    """upsert 一条 APISIX 路由（id=version_id）→ dispatcher + 注入 X-API-Version-Id。"""
    settings = get_settings()
    if not settings.apisix_admin_url:
        raise ApiError(ErrorCode.INTERNAL, "APISIX_ADMIN_URL not configured", http_status=500)

    uri = (base_path.rstrip("/") + _normalize_path(path)) if base_path else _normalize_path(path)
    body = {
        "uri": uri,
        "methods": [method.upper()],
        "upstream": {"type": "roundrobin", "nodes": {settings.dispatcher_upstream: 1}},
        "plugins": {
            "proxy-rewrite": {
                "regex_uri": ["^/(.*)$", "/dispatch/$1"],
                "headers": {"set": [f"X-API-Version-Id: {version_id}"]},
            }
        },
    }
    await _admin_request(
        "PUT",
        f"{settings.apisix_admin_url}/apisix/admin/routes/{version_id}",
        json=body,
    )


async def retire_route(version_id: str) -> None:
    """占位 —— R1c 设计：retire 不删路由（dispatcher 按 retired 状态返 410）。

    保留供后续 stale 路由清理 follow-up 使用；R1c 内 retire handler 不调用本函数。
    """
    return None
```

- [ ] **Step 5: 跑测试确认通过**

Run: `.venv/bin/python -m pytest services/services/api-registry/tests/test_apisix_client.py -v`
Expected: PASS（3 passed）。

- [ ] **Step 6: lint + commit**

```bash
.venv/bin/python -m ruff check services/services/api-registry/src/api_registry/apisix_client.py services/services/api-registry/tests/test_apisix_client.py services/libs/apihub-core/src/apihub_core/config.py
git add -A && git commit -m "R1c §1: api-registry apisix_client + dispatcher_upstream 配置"
```

---

## Task 2: api-registry publish 接线 + retire 注释 + 单测

**Files:**
- Modify: `services/services/api-registry/src/api_registry/routes.py`（publish handler `:135-164`、retire handler `:196-228`、import 块）
- Test: `services/services/api-registry/tests/test_lifecycle.py`（新增/扩展 publish 用例，仿既有 publish 测试的 db stub 模式）

**Interfaces:**
- Consumes: Task 1 的 `publish_route`。

- [ ] **Step 1: 先读 `test_lifecycle.py` 既有的 publish 测试**，确认它如何 stub `db_session`（DB 触达），照搬该模式新增「publish 先调 publish_route」用例。**实现者必读**：`services/services/api-registry/tests/test_lifecycle.py`。

- [ ] **Step 2: 写失败测试**（在 `test_lifecycle.py`，仿既有 publish 测试；stub `apisix_client.publish_route` 捕获调用 + stub db）

```python
async def test_publish_calls_apisix_before_status(admin_client, monkeypatch):
    """publish 先下发 APISIX 路由，成功才置 published。"""
    # 照搬既有 publish 测试的 db stub（让 fetchrow 返回 draft/reviewing 的 api_version
    # + 暴露 base_path），再补：
    captured = {}

    async def _fake_publish(*, version_id, method, path, base_path):
        captured.update(version_id=version_id, method=method, path=path, base_path=base_path)

    from api_registry import apisix_client
    monkeypatch.setattr(apisix_client, "publish_route", _fake_publish)
    # …既有 db stub 让 SELECT api_version 命中 + 提供 api.base_path…
    # r = await admin_client.post("/v1/api-versions/<ver>/publish")
    # assert r.status_code == 200
    # assert captured["version_id"] == "<ver>"
    # assert captured["method"] == "<from row>"
    # assert captured["base_path"] == "<from api 表>"
```

> **注**：本步要求实现者先把既有 publish 测试的 db stub 抄进来（精确签名以 `test_lifecycle.py` 现状为准），再叠加 `publish_route` stub。`base_path` 来自 api 表——publish handler 需 join/补查（见 Step 3）。

- [ ] **Step 3: 改 publish handler**（`routes.py:135-164`）

(3a) 顶部 import 加：`from api_registry import apisix_client`（`get_settings` 若无其他用处则不必加）。

(3b) publish handler：`SELECT * FROM api_version` 改为带 api.base_path 的 join，并在 `UPDATE status='published'` **之前**调 `publish_route`：

```python
        require_tenant()
        async with db.db_session() as conn:
            row = await conn.fetchrow(
                """
                SELECT v.*, a.base_path
                FROM api_version v JOIN api a ON a.id = v.api_id
                WHERE v.id = $1 AND v.status IN ('draft', 'reviewing')
                """,
                version_id,
            )
            if not row:
                raise ApiError(ErrorCode.API_NOT_PUBLISHED, "Version not publishable")

            # 先下发 APISIX 路由，成功才置 published（避免 DB published 但数据面无路由的窗口）
            await apisix_client.publish_route(
                version_id=version_id,
                method=row["method"],
                path=row["path"],
                base_path=row["base_path"],
            )

            await conn.execute(
                "UPDATE api_version SET status = 'published', published_at = NOW() WHERE id = $1",
                version_id,
            )
```

（后续 kafka.emit / return 不变。）

- [ ] **Step 4: 改 retire handler**（`routes.py:196-228`）——去掉 TODO，加说明注释（不调 apisix_client）

把 `# TODO: 摘除 APISIX 路由（调用方将收到 410 Gone）` 替换为：

```python
        # retire 不摘除 APISIX 路由：dispatcher 按 status='retired' 返 410 Gone
        # （避免启用 APISIX serverless 410 插件的 helm upgrade）。stale 路由清理见 follow-up。
```

- [ ] **Step 5: 跑测试**

Run: `.venv/bin/python -m pytest services/services/api-registry/tests/test_lifecycle.py -v`
Expected: PASS（含新 publish 用例 + 既有；若既有用例因 base_path join 改动受影响，相应调整 stub 让 row 含 `base_path`）。

- [ ] **Step 6: lint + commit**

```bash
.venv/bin/python -m ruff check services/services/api-registry/
git add -A && git commit -m "R1c §2: publish 下发 APISIX 路由（base_path join + 先下发后置 published）"
```

---

## Task 3: dispatcher 退纯转发 + 生命周期→状态映射 + 单测

**Files:**
- Modify: `services/libs/apihub-core/src/apihub_core/errors.py`（加 `API_RETIRED`→410）
- Modify: `services/services/dispatcher/src/dispatcher/resolver.py`（`resolve_by_header` 放开状态 + retired→410；删 `resolve_by_path` + `_match_path`）
- Modify: `services/services/dispatcher/src/dispatcher/routes.py`（`dispatch` 强制 header；删 else 分支）
- Test: `services/services/dispatcher/tests/test_resolver_lifecycle.py`（新增，仿 `async_client` fixture）

**Interfaces:**
- Produces: `/dispatch` 要求 `X-API-Version-Id`；`resolve_by_header` 接受 published+deprecated，retired→410。

- [ ] **Step 1: 加 `ErrorCode.API_RETIRED`**

`errors.py` 接口段（`API_DOWN = 30004` 后）加：

```python
    API_RETIRED = 30005
```

`_HTTP_STATUS_MAP` 加：

```python
    ErrorCode.API_RETIRED: 410,
```

- [ ] **Step 2: 写失败测试**（新文件 `services/services/dispatcher/tests/test_resolver_lifecycle.py`）

```python
"""resolve_by_header 生命周期映射 + /dispatch 强制 header。"""

import pytest
from apihub_core.errors import ApiError, ErrorCode


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch):
    from apihub_core import redis as redis_mod

    async def _miss(key):
        return None

    monkeypatch.setattr(redis_mod, "t_get", _miss)
    yield


async def _row(status):
    return {
        "id": "ver_1", "api_id": "api_1", "tenant_id": "t1", "version": "v1",
        "backend_type": "http", "backend_url": "http://up/v1", "method": "GET",
        "path": "/x", "masking": None, "rate_limit": None, "retry_policy": None,
        "cache_policy": None, "ai_model": None, "ai_streaming": False, "ai_params": None,
        "sla_p99_ms": None, "sla_availability": None, "status": status,
    }


def _meta_session(fetchrow):
    class _CM:
        async def __aenter__(self):
            return type("C", (), {"fetchrow": staticmethod(fetchrow),
                                  "fetchval": staticmethod(lambda *a, **k: None)})()

        async def __aexit__(self, *e):
            return False

    async def _factory(*a, **k):
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

    async def _factory(*a, **k):
        return _CM()

    monkeypatch.setattr(resolver.db, "meta_db_session", _factory)
    with pytest.raises(ApiError) as ei:
        await resolver.resolve_by_header("ver_1")
    assert ei.value.code == ErrorCode.API_RETIRED
    assert ei.value.http_status == 410


async def test_dispatch_missing_header_returns_400(async_client):
    """resolve_by_path 已删；无 X-API-Version-Id → 400。"""
    r = await async_client.get("/dispatch/v1/x")
    assert r.status_code == 400


async def test_resolve_by_path_removed():
    from dispatcher import resolver

    assert not hasattr(resolver, "resolve_by_path")
```

- [ ] **Step 3: 跑测试确认失败**

Run: `.venv/bin/python -m pytest services/services/dispatcher/tests/test_resolver_lifecycle.py -v`
Expected: FAIL（`API_RETIRED` 不存在；`resolve_by_header` 仍只查 published；`resolve_by_path` 仍在）。

- [ ] **Step 4: 改 `resolve_by_header`**（`resolver.py:23-47`）

```python
async def resolve_by_header(version_id: str) -> ApiVersionSnapshot:
    """APISIX 注入的 X-API-Version-Id → 按 ID 查 + Redis 缓存。

    生命周期：published/deprecated 可路由；retired → 410 Gone；其余 → 404。
    """
    cache_key = f"snapshot:{version_id}"
    cached = await redis.t_get(cache_key)
    if cached:
        return _from_json(json.loads(cached))

    async with db.meta_db_session() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, api_id, tenant_id, version, backend_type, backend_url,
                   method, path, masking, rate_limit, retry_policy, cache_policy,
                   ai_model, ai_streaming, ai_params, sla_p99_ms, sla_availability
            FROM api_version
            WHERE id = $1 AND status IN ('published', 'deprecated')
            """,
            version_id,
        )
        if not row:
            status = await conn.fetchval(
                "SELECT status FROM api_version WHERE id = $1", version_id
            )
            if status == "retired":
                raise ApiError(ErrorCode.API_RETIRED, f"version {version_id} retired")
            raise ApiError(ErrorCode.API_NOT_PUBLISHED, f"version {version_id} not published")

    _, visibility = await _get_api_meta(row["api_id"])
    snapshot = _from_row(row, visibility=visibility)
    await redis.t_set(cache_key, json.dumps(dataclasses.asdict(snapshot)), ex=300)
    return snapshot
```

- [ ] **Step 5: 删 `resolve_by_path` + `_match_path`**

删除 `resolver.py` 中 `resolve_by_header` 之后的整个 `resolve_by_path`（`:50-74`）与 `_match_path`（`:95-110`，若仅 resolve_by_path 用）。`require_tenant` import 若变为未用，ruff 会提示——确认 routes.py 仍用则保留（dispatch 已不调；但其他 handler 可能用），按 ruff 处理。

- [ ] **Step 6: 改 `dispatch` handler**（`routes.py:64-107`）——强制 header，删 else 分支

```python
    async def dispatch(request: Request):
        version_id = request.headers.get("X-API-Version-Id")
        if not version_id:
            raise ApiError(
                ErrorCode.INVALID_PARAMS,
                "missing X-API-Version-Id (must enter via APISIX)",
                http_status=400,
            )
        rest = request.path_params["rest"]
        snap = await resolve_by_header(version_id)

        from apihub_core.tenant import get_tenant_context
        from dispatcher.visibility import check_visibility

        ctx = get_tenant_context()
        if ctx is not None:
            check_visibility(snap, ctx)

        if snap.backend_type == "async_task":
            return await dispatch_async_task(snap, request)
        if snap.backend_type == "workflow":
            raise ApiError(
                ErrorCode.INTERNAL,
                "workflow backend: use POST /v1/jobs (not /dispatch)",
                http_status=501,
            )
        if request.headers.get("X-Environment", "").lower() == "sandbox":
            from dataclasses import replace
            snap = replace(snap, backend_url=f"http://mock-backend.apihub-system/dispatch{rest}")
        return await get_forwarder().forward(snap, request)
```

并从 `routes.py` 顶部 import 删除 `resolve_by_path`（若按名 import）。

- [ ] **Step 7: 跑测试**

Run: `.venv/bin/python -m pytest services/services/dispatcher/tests/ -v`
Expected: PASS（含新 lifecycle 用例；既有 dispatcher 测试若依赖 resolve_by_path 或无 header 直连，相应改造为带 header）。

- [ ] **Step 8: lint + commit**

```bash
.venv/bin/python -m ruff check services/libs/apihub-core/src/apihub_core/errors.py services/services/dispatcher/
git add -A && git commit -m "R1c §3: dispatcher 退纯转发（删 resolve_by_path + 强制 header + retired→410）"
```

---

## Task 4: k8s configmap + .env.dev + 文档

**Files:**
- Modify: `deploy/k8s/services/api-registry/configmap.yaml`（加 `DISPATCHER_UPSTREAM` + Secret 占位注释）
- Modify: `.env.dev`（加 `APISIX_ADMIN_KEY` + `DISPATCHER_UPSTREAM`）
- Modify: `docs/aggregate-ownership.md`（路由归属条款）
- Modify: `docs/03-services.md`（§3.1/§3.2 数据面路径修正）

- [ ] **Step 1: configmap** —— 在 `APISIX_ADMIN_URL` 行后加 `DISPATCHER_UPSTREAM: dispatcher.apihub-system:8001`；Secret 块注释加 `#   APISIX_ADMIN_KEY: <from sealed secret>`。

- [ ] **Step 2: `.env.dev`** —— 加：
```
APISIX_ADMIN_KEY=<kind 部署实际值，实现者 kubectl -n apihub-ingress 取>
DISPATCHER_UPSTREAM=dispatcher.apihub-system:8001
```

- [ ] **Step 3: `docs/aggregate-ownership.md`** —— 追加条款：「**路由归属 = APISIX**：动态路由 + 注入 X-API-Version-Id；dispatcher 是纯转发（不做 path 解析）；api-registry 通过 APISIX Admin API 下发 publish。」

- [ ] **Step 4: `docs/03-services.md` §3.1/§3.2** —— 注明 publish→APISIX Admin API 下发路由、dispatcher 依赖 `X-API-Version-Id` 的真实数据面路径（替代「手动静态 route」过时描述）。

- [ ] **Step 5: commit**

```bash
git add -A && git commit -m "R1c §4: configmap(DISPATCHER_UPSTREAM) + .env.dev + 文档（路由归属=APISIX）"
```

---

## Task 5: kind e2e + 回归 + lint 收口

**Files:** 无（验证）。

- [ ] **Step 1: 取 kind APISIX admin key**

```bash
kubectl -n apihub-ingress get secret -o name | head   # 找 apisix admin secret
helm -n apihub-ingress get values apisix 2>/dev/null | grep -i key
```

- [ ] **Step 2: 部署 / port-forward** —— api-registry + dispatcher 指向 kind（或 port-forward APISIX admin `9180` + gateway）。确认 `APISIX_ADMIN_KEY`/`APISIX_ADMIN_URL`/`DISPATCHER_UPSTREAM` 注入。

- [ ] **Step 3: e2e 流程**
1. 经 api-registry publish 一个测试 api_version（draft→published）。
2. `curl -H "X-API-KEY: <admin>" http://localhost:9180/apisix/admin/routes/<version_id>` → 确认路由存在、uri/method/upstream/proxy-rewrite 正确。
3. 经 APISIX gateway（带有效 consumer API Key）调该路径 → 200。
4. deprecate → 再调 → 仍 200。
5. retire → 再调 → **410**。

- [ ] **Step 4: 回归 + lint**

```bash
.venv/bin/python -m pytest services/services/api-registry/tests/ services/services/dispatcher/tests/ services/libs/apihub-core/tests/ -v
.venv/bin/python -m ruff check services/
```
Expected: 测试全绿（除预存基线）；ruff 0 新增。

- [ ] **Step 5: PR-ready 自检** —— `git log --oneline main..HEAD` 仅含 R1c 改动；未 push（等用户要求）。

> e2e 若 kind 环境受限跑不通，至少完成 Step 4 单测回归，并在 PR 描述注明 e2e 待人工补；不得伪造 e2e 结果。

---

## Self-Review（plan 作者自查，已做）

- **Spec 覆盖**：spec ①apisix_client=Task1；②publish/retire 接线=Task2；③dispatcher 清理=Task3；④配置=Task4；⑤文档=Task4；验证=Task5。✅
- **占位符**：Task2 的 db stub 以「仿既有 test_lifecycle.py」指明（实现者读该文件）——因确切签名取决于既有代码，已点名必读文件，非空泛 TBD。其余步骤含完整代码。✅
- **一致性**：`publish_route(*, version_id, method, path, base_path)` 签名 Task1 定义、Task2 调用一致；`ErrorCode.API_RETIRED`(30005→410) Task3 定义+测试断言一致；`dispatcher_upstream` Task1(config.py)+Task4(configmap) 一致。✅
- **风险落地**：删 resolve_by_path 破 dev 直连 → Task3 Step7「既有测试改造为带 header」；APISIX uri 语法 → Task5 e2e 验证；admin key → Task5 Step1 取值。✅
