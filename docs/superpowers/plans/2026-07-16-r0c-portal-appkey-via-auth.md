# R0c — portal app/key 走 auth（服务边界第一步）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 portal-bff 对 `app`/`api_key` 表的直写/直读改为转发 auth API，消除 §9-B 服务边界泄漏；补齐 auth 缺失的 app 管理端点；落一份聚合所有权硬规则文档。

**Architecture:** 薄 BFF 转发——portal 把用户 `Authorization: Bearer <JWT>` 原样转发给 auth（与现有 `/account`、`/consent` 转发同款）。auth 中间件本地验签 JWT 注入 TenantContext，handler 用 `ctx.tenant_id` 建 app/key（RLS 兜底）。auth 现仅有 key 端点，需补 `POST/GET /v1/apps`。无需新凭证，租户来自 JWT 不可伪造。

**Tech Stack:** Python 3.11 / FastAPI / asyncpg（直连，非 SQLAlchemy）/ pydantic v2 / httpx / pytest（asyncio_mode=auto）。

**Spec:** `docs/superpowers/specs/2026-07-16-r0c-portal-appkey-via-auth-design.md`

## Global Constraints

- **RLS 是中央不变量**：业务读写一律走 `db_session()`（内部 `SET LOCAL app.tenant_id`）。auth 新端点用 `require_tenant()` 取 `ctx.tenant_id`，不从请求体取租户。
- **禁 smoke 脚本绕生产者**：验证走 HTTP 真实入口 + 单测，不手动注入数据。
- **测试约定**：async（无需 `@pytest.mark.asyncio`）；`httpx.ASGITransport(app)` 打 app；monkeypatch `apihub_core.auth.authenticate_request` 注入固定 `TenantContext`；DB 触达的 repo 函数在路由单测里一并 mock（auth 约定：`monkeypatch.setattr(r, "X", fake)` + `routes_mod.X = r.X`，因 routes.py 按名 import repo 函数）。
- **命名防自调用**：auth `routes.py` 按名 import repo 函数；route handler 不得与 repo 函数同名（否则 `await create_app()` 递归调自己）。本计划 route handler 用 `create_app_route`/`list_apps_route`，repo 用 `create_app`/`list_apps_for_tenant`。
- **Lint**：根 `pyproject.toml`（ruff: E/F/I/B/UP/SIM/C4/ASYNC/S；mypy 非严格）。删函数后注意清 unused import（F401）。
- **提交节奏**：每个 Task 末尾 commit；一轮一个 squash-PR；push/merge 仅在用户要求时。

**表结构依据**（`scripts/init-db/01-schema.sql:62-90`）：
- `app(id text PK, tenant_id text, name text, type text CHECK IN('internal','external','web','mobile','server') default 'internal', status text default 'active', quota_tier, metadata jsonb, created_at, updated_at)`。
- `api_key(id, tenant_id, app_id, key_prefix, key_hash, name, scopes text[], status, last_used_at, expires_at, created_at, revoked_at, revoked_reason)`。

---

## Task 1: auth 补 app 管理端点（POST/GET /v1/apps）

**Files:**
- Modify: `services/services/auth/src/auth/models.py`（加 `AppCreate`/`AppResponse`）
- Modify: `services/services/auth/src/auth/repository.py`（追加 `create_app`/`list_apps_for_tenant`）
- Modify: `services/services/auth/src/auth/routes.py`（import 块 + 2 个 route handler）
- Test: `services/services/auth/tests/test_routes.py`（加 `TestCreateApp`/`TestListApps`）

**Interfaces:**
- Produces（后续 Task 2 依赖）: auth 端点 `POST /v1/apps`（body `AppCreate`→`AppResponse`，status 200）、`GET /v1/apps`（→`list[AppResponse]`）。两个端点走标准中间件，portal 将转发 `Authorization: Bearer` 到此。`POST /v1/apps/{app_id}/api-keys` 已存在（`routes.py:129`），Task 2 直接复用。

- [ ] **Step 1: 写失败测试 —— `TestCreateApp` / `TestListApps`**

在 `services/services/auth/tests/test_routes.py` 末尾追加（`routes_mod` 已在文件顶部 import 为 `from auth import routes as routes_mod`）：

```python
# ========== /v1/apps (受保护端点，app 管理) ==========


class TestCreateApp:
    async def test_requires_auth(self, client):
        """无凭证 → 中间件 401。"""
        resp = await client.post("/v1/apps", json={"name": "my app"})
        assert resp.status_code == 401

    async def test_creates_app_with_caller_tenant(self, client, authed, monkeypatch):
        """鉴权通过 → 建 app，tenant_id 来自 ctx（authed fixture 的 t1），不是请求体。"""
        captured = {}

        async def _create(**kwargs):
            captured.update(kwargs)
            return {
                "id": kwargs["app_id"],
                "name": kwargs["name"],
                "tenant_id": kwargs["tenant_id"],
                "type": kwargs["app_type"],
                "status": "active",
            }

        from auth import repository as r

        monkeypatch.setattr(r, "create_app", _create)
        routes_mod.create_app = r.create_app

        resp = await client.post(
            "/v1/apps",
            json={"name": "my app", "type": "external"},
            headers={"Authorization": "Bearer eyJtest"},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "my app"
        assert body["status"] == "active"
        assert body["type"] == "external"
        assert body["id"].startswith("app_")
        # 关键：tenant_id 来自 ctx，不是请求体
        assert captured["tenant_id"] == "t1"
        assert captured["name"] == "my app"
        assert captured["app_type"] == "external"
        assert captured["app_id"].startswith("app_")


class TestListApps:
    async def test_requires_auth(self, client):
        resp = await client.get("/v1/apps")
        assert resp.status_code == 401

    async def test_lists_only_caller_tenant(self, client, authed, monkeypatch):
        captured = {}

        async def _list(tenant_id):
            captured["tenant_id"] = tenant_id
            return [
                {
                    "id": "app_a",
                    "name": "A",
                    "tenant_id": tenant_id,
                    "type": "external",
                    "status": "active",
                }
            ]

        from auth import repository as r

        monkeypatch.setattr(r, "list_apps_for_tenant", _list)
        routes_mod.list_apps_for_tenant = r.list_apps_for_tenant

        resp = await client.get(
            "/v1/apps", headers={"Authorization": "Bearer eyJtest"}
        )

        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["id"] == "app_a"
        assert captured["tenant_id"] == "t1"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest services/services/auth/tests/test_routes.py::TestCreateApp services/services/auth/tests/test_routes.py::TestListApps -v`
Expected: FAIL（`AppResponse` 不存在 / 路由 404 / `create_app` 不存在）。401 的那两条可能先过（中间件拦截），其余失败。

- [ ] **Step 3: 加模型 `AppCreate` / `AppResponse`**

在 `services/services/auth/src/auth/models.py` 的 `from pydantic import ...` 之后、`class ApiKeyCreate` 之前插入：

```python
class AppCreate(BaseModel):
    """创建 app 请求（portal 转发；调用方 tenant 来自中间件 JWT/APIKey ctx）。"""

    name: str = Field(min_length=2, max_length=64)
    type: str = "external"


class AppResponse(BaseModel):
    """app 响应（字段对齐 portal 契约）。"""

    id: str
    name: str
    tenant_id: str
    type: str
    status: str


```

- [ ] **Step 4: 加 repo 函数 `create_app` / `list_apps_for_tenant`**

在 `services/services/auth/src/auth/repository.py` 文件末尾追加（`db`、`ApiError` 已 import；无新 import——id 在 route 里生成）：

```python


async def create_app(
    *, app_id: str, tenant_id: str, name: str, app_type: str
) -> dict:
    """插入新 app（同租户 RLS 由 db_session 的 SET LOCAL app.tenant_id 保证）。"""
    async with db.db_session() as conn:
        await conn.execute(
            """
            INSERT INTO app (id, tenant_id, name, type, status)
            VALUES ($1, $2, $3, $4, 'active')
            """,
            app_id,
            tenant_id,
            name,
            app_type,
        )
    return {
        "id": app_id,
        "name": name,
        "tenant_id": tenant_id,
        "type": app_type,
        "status": "active",
    }


async def list_apps_for_tenant(tenant_id: str) -> list[dict]:
    """列出本租户所有 app（RLS 过滤）。"""
    async with db.db_session() as conn:
        rows = await conn.fetch(
            "SELECT id, name, tenant_id, type, status FROM app "
            "WHERE tenant_id = $1 ORDER BY created_at DESC",
            tenant_id,
        )
    return [dict(r) for r in rows]
```

- [ ] **Step 5: 在 `auth/routes.py` 接线（import + route handler）**

(5a) 修改 repository import 块（`routes.py:35-41`），加 `create_app`、`list_apps_for_tenant`：

```python
from auth.repository import (
    create_api_key,
    create_app,
    get_tenant_home_region,
    list_api_keys_for_app,
    list_apps_for_tenant,
    revoke_api_key,
    verify_api_key_record,
)
```

(5b) 修改 models import 块（`routes.py:20-34`），在列表里加 `AppCreate`、`AppResponse`（保持字母序）：

```python
from auth.models import (
    ApiKeyCreate,
    ApiKeyListItem,
    ApiKeyResponse,
    AppCreate,
    AppResponse,
    AuthResponse,
    ConsentResponse,
    ConsentWithdrawResponse,
    DeleteAccountResponse,
    ExportResponse,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    VerifyRequest,
    VerifyResponse,
)
```

（仅新增 `AppCreate,` 与 `AppResponse,` 两行；其余原样。）

(5c) 在 `revoke_key` 之后、`# ========== 外部开发者身份端点` 注释之前（约 `routes.py:187-189` 之间）插入两个 route handler。**handler 名带 `_route` 后缀避免与 import 进来的 repo 函数 `create_app` 同名递归**：

```python
    @app.post("/v1/apps", response_model=AppResponse)
    async def create_app_route(payload: AppCreate):
        """创建 app。调用方 tenant 来自中间件注入的 ctx（JWT 或 APIKey）。"""
        ctx = require_tenant()
        app_id = f"app_{uuid.uuid4().hex[:16]}"
        record = await create_app(
            app_id=app_id,
            tenant_id=ctx.tenant_id,
            name=payload.name,
            app_type=payload.type,
        )
        log.info("app_created", app_id=app_id, tenant_id=ctx.tenant_id)
        return AppResponse(**record)

    @app.get("/v1/apps", response_model=list[AppResponse])
    async def list_apps_route():
        """列出本租户所有 app。"""
        ctx = require_tenant()
        rows = await list_apps_for_tenant(ctx.tenant_id)
        return [AppResponse(**r) for r in rows]
```

（`uuid`、`log`、`require_tenant` 均已在 `routes.py` 顶部 import，无需新增。）

- [ ] **Step 6: 跑测试确认通过**

Run: `pytest services/services/auth/tests/test_routes.py -v`
Expected: PASS（含新增 4 条 + 既有全部）。

- [ ] **Step 7: lint**

Run: `ruff check services/services/auth/src/auth/ services/services/auth/tests/`
Expected: 无 error（注意 import 排序——上面 import 块已按字母序）。

- [ ] **Step 8: Commit**

```bash
git add services/services/auth/src/auth/models.py services/services/auth/src/auth/repository.py services/services/auth/src/auth/routes.py services/services/auth/tests/test_routes.py
git commit -m "R0c §1: auth 补 app 管理端点 (POST/GET /v1/apps)"
```

---

## Task 2: portal 删直写、改转发 auth + 重写测试

**Files:**
- Modify: `services/services/portal/src/portal/routes.py`（3 个 handler 改 `_forward` 转发 + 字段映射）
- Modify: `services/services/portal/src/portal/repository.py`（删 3 个直写函数 + 清 unused import + 改模块 docstring）
- Test: `services/services/portal/tests/test_routes.py`（重写 3 个 app/key 测试为 httpx 转发断言；删 1 个测已删 repo 函数的用例）

**Interfaces:**
- Consumes: Task 1 的 `POST /v1/apps`、`GET /v1/apps`，及既有 `POST /v1/apps/{app_id}/api-keys`。
- Produces: portal 三个端点行为不变（契约同旧），但内部不再触达 PG：`POST /v1/portal/apps` (201, `AppResponse`)、`GET /v1/portal/apps` (`list[AppResponse]`)、`POST /v1/portal/apps/{app_id}/api-keys` (201, `ApiKeyResponse`，`key_prefix` 映射自 auth `display_prefix`)。

- [ ] **Step 1: 重写 portal app/key 测试为「转发到 auth」断言（先失败）**

打开 `services/services/portal/tests/test_routes.py`，**删除**以下旧用例（它们 monkeypatch 即将被删除的 repo 函数，会直接报错）：
- `test_create_app_uses_caller_tenant`（约 L4-28）
- `test_list_apps`（约 L31-39）
- `test_create_api_key`（约 L42-67）
- `test_create_api_key_for_app_rejects_foreign_app`（约 L163-195，测的是将被删除的 `create_api_key_for_app`）

在文件顶部（旧 `test_create_app_uses_caller_tenant` 的位置）改为下面三个新用例。`client` fixture（conftest）已带 `Authorization: Bearer eyJ.test.token` 并把 tenant 设为 `external-public`：

```python
"""portal-bff 路由单测（app/key 走转发，mock httpx；其余 mock repository）。"""


async def test_create_app_forwards_to_auth(client, monkeypatch):
    """POST /v1/portal/apps 转发用户 JWT 到 auth /v1/apps，不再直写 PG。"""
    import httpx as _httpx

    real_async_client = _httpx.AsyncClient
    captured = {}

    class _FakeResp:
        status_code = 200  # auth POST /v1/apps 返回 200

        def json(self):
            return {
                "id": "app_new",
                "name": "my app",
                "tenant_id": "external-public",
                "type": "external",
                "status": "active",
            }

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

    monkeypatch.setattr(_httpx, "AsyncClient", _FakeClient)

    r = await client.post(
        "/v1/portal/apps", json={"name": "my app", "type": "external"}
    )
    assert r.status_code == 201  # portal 契约 201
    assert r.json()["id"] == "app_new"
    # 转发到 auth /v1/apps，无 /v1/v1/ 双前缀
    assert captured["url"] == "http://auth.apihub-system/v1/apps", captured["url"]
    assert captured["method"] == "POST"
    # 用户 JWT 原样转发
    assert captured["headers"]["Authorization"] == "Bearer eyJ.test.token"
    assert captured["json"] == {"name": "my app", "type": "external"}


async def test_list_apps_forwards_to_auth(client, monkeypatch):
    """GET /v1/portal/apps 转发 auth /v1/apps。"""
    import httpx as _httpx

    captured = {}

    class _FakeResp:
        status_code = 200

        def json(self):
            return [
                {
                    "id": "app_a",
                    "name": "A",
                    "tenant_id": "external-public",
                    "type": "external",
                    "status": "active",
                }
            ]

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def request(self, method, url, **kw):
            captured["url"] = url
            captured["method"] = method
            return _FakeResp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(_httpx, "AsyncClient", _FakeClient)

    r = await client.get("/v1/portal/apps")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["id"] == "app_a"
    assert captured["url"] == "http://auth.apihub-system/v1/apps"
    assert captured["method"] == "GET"


async def test_create_api_key_forwards_and_maps_prefix(client, monkeypatch):
    """POST /v1/portal/apps/{id}/api-keys 转发 auth，并把 display_prefix→key_prefix。"""
    import httpx as _httpx

    captured = {}

    class _FakeResp:
        status_code = 200

        def json(self):
            return {
                "id": "key_new",
                "app_id": "app_x",
                "name": "prod key",
                "scopes": [],
                "api_key": "ak_supersecret",
                "display_prefix": "ak_abcd12",
                "expires_at": None,
                "created_at": "2026-07-16T00:00:00",
            }

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def request(self, method, url, **kw):
            captured["url"] = url
            captured["json"] = kw.get("json")
            return _FakeResp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(_httpx, "AsyncClient", _FakeClient)

    r = await client.post(
        "/v1/portal/apps/app_x/api-keys", json={"name": "prod key"}
    )
    assert r.status_code == 201  # portal 契约 201
    body = r.json()
    assert body["api_key"] == "ak_supersecret"
    assert body["key_prefix"] == "ak_abcd12"  # 映射自 auth display_prefix
    assert "display_prefix" not in body  # portal 不暴露 auth 原字段
    assert captured["url"] == "http://auth.apihub-system/v1/apps/app_x/api-keys"
    assert captured["json"] == {"name": "prod key"}
```

> 注意：保留 `test_auth_endpoints_skip_auth_paths`、`test_forward_composes_correct_auth_url`、`test_list_portal_apis` 等其余用例不动。`test_create_api_key_for_app_rejects_foreign_app` 整体删除（被测函数即将删除；归属校验已由 auth 侧 `create_api_key` repo + RLS 覆盖，见 auth `test_routes.py::TestCreateKey`）。

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest services/services/portal/tests/test_routes.py -v`
Expected: 新增三条 FAIL（当前 handler 仍调 `repository.create_app_for_user` 等，未走 httpx 转发 → 返回值形状/行为对不上）。`test_forward_composes_correct_auth_url` 等仍 PASS。

- [ ] **Step 3: 改 `portal/routes.py` 三个 handler 为转发**

(3a) `create_app`（约 `routes.py:235-240`）改为：

```python
    @app.post("/v1/portal/apps", response_model=AppResponse, status_code=201)
    async def create_app(request: Request, payload: AppCreate):
        """建 app —— 转发用户 JWT 到 auth /v1/apps（不再直写 app 表）。"""
        require_tenant()
        st, body = await _forward(
            "POST",
            "/v1/apps",
            headers={"Authorization": request.headers.get("Authorization", "")},
            json={"name": payload.name, "type": payload.type},
        )
        if st >= 400:
            raise ApiError(ErrorCode.INTERNAL, f"auth error: {body}", http_status=st)
        return AppResponse(**body)
```

(3b) `list_apps`（约 `routes.py:242-245`）改为：

```python
    @app.get("/v1/portal/apps", response_model=list[AppResponse])
    async def list_apps(request: Request):
        """列本租户 app —— 转发 auth /v1/apps。"""
        require_tenant()
        st, body = await _forward(
            "GET",
            "/v1/apps",
            headers={"Authorization": request.headers.get("Authorization", "")},
        )
        if st >= 400:
            raise ApiError(ErrorCode.INTERNAL, f"auth error: {body}", http_status=st)
        return [AppResponse(**a) for a in body]
```

(3c) `create_api_key`（约 `routes.py:247-256`）改为（含 `display_prefix→key_prefix` 映射）：

```python
    @app.post(
        "/v1/portal/apps/{app_id}/api-keys",
        response_model=ApiKeyResponse,
        status_code=201,
    )
    async def create_api_key(request: Request, app_id: str, payload: ApiKeyCreate):
        """建 APIKey —— 转发 auth，明文 key 仅此次返回。"""
        require_tenant()
        st, body = await _forward(
            "POST",
            f"/v1/apps/{app_id}/api-keys",
            headers={"Authorization": request.headers.get("Authorization", "")},
            json={"name": payload.name},
        )
        if st >= 400:
            raise ApiError(ErrorCode.INTERNAL, f"auth error: {body}", http_status=st)
        return ApiKeyResponse(
            id=body["id"],
            app_id=body["app_id"],
            name=body["name"],
            key_prefix=body["display_prefix"],  # 映射 auth 字段
            api_key=body["api_key"],
        )
```

> `Request`、`ApiError`、`ErrorCode`、`AppCreate`/`AppResponse`/`ApiKeyCreate`/`ApiKeyResponse` 均已在 `portal/routes.py` 顶部 import（L6-11）。`_forward` 与 `auth_base` 在 `register_routes` 闭包内已定义（L21-29）。

- [ ] **Step 4: 删 `portal/repository.py` 的三个直写函数 + 清 import + 改 docstring**

(4a) 模块 docstring（L1-4）改为（去掉"直写 app/api_key 表"的过时描述）：

```python
"""portal 聚合层 —— API 目录/在线调试/计费只读。

app/key 自助已改走 auth API（见 routes.py 转发），本模块不再触达 app/api_key 表。
"""
# ruff: noqa: S608
```

(4b) 删除 `import secrets`（L7）—— 删完下面三函数后全文件无 `secrets.` 引用，保留会触发 F401。

(4c) 删除三个函数整体：
- `create_app_for_user`（L25-44）
- `list_apps_for_user`（L47-54）
- `create_api_key_for_app`（L57-92，含其内部的 `from auth.apikey import generate_api_key` 局部 import）

> 保留：`list_portal_apis`、`get_api_detail`、`try_api`、计费相关（`get_billing_summary`/`list_plans`/`get_subscription`/`subscribe_plan`/`get_invoices`）—— 非 app/key 聚合，不动。`from portal.models import (...)`（L15-22）不引用 App/ApiKey 模型，无需改。

- [ ] **Step 5: 跑 portal 测试确认通过**

Run: `pytest services/services/portal/tests/test_routes.py -v`
Expected: PASS（含三条新转发用例 + 既有 `test_forward_composes_correct_auth_url` 等）。

- [ ] **Step 6: 边界硬断言 —— portal 再无 app/api_key 表直写直读**

Run: `grep -rnE "INSERT INTO (app|api_key)\b|FROM (app|api_key)\b" services/services/portal/ ; echo "exit=$?"`
Expected: 无任何输出，`exit=1`（grep 无匹配）= 通过。若有输出，说明仍有遗漏的直写直读，必须清掉。

- [ ] **Step 7: lint**

Run: `ruff check services/services/portal/src/portal/ services/services/portal/tests/`
Expected: 无 error（重点确认 `secrets` 未残留 F401；`repository` 仍被 `routes.py` 引用故保留）。

- [ ] **Step 8: Commit**

```bash
git add services/services/portal/src/portal/routes.py services/services/portal/src/portal/repository.py services/services/portal/tests/test_routes.py
git commit -m "R0c §2: portal app/key 改走 auth API（删直写 app/api_key 表）"
```

---

## Task 3: 聚合所有权文档 + CLAUDE.md 引用

**Files:**
- Create: `docs/aggregate-ownership.md`
- Modify: `CLAUDE.md`（Architecture 节加一行引用）

**Interfaces:** 无代码接口。产出治理文档，作为 §9-B 架构护栏，约束后续轮次（admin→audit 等）。

- [ ] **Step 1: 写 `docs/aggregate-ownership.md`**

```markdown
# 聚合所有权（Aggregate Ownership）

> 硬规则：**BFF（portal / admin）是聚合/转发层，不得直写领域服务的表；跨聚合只能走拥有方 API。**
> 这是 `docs/phase4-audit-findings.md` §9-B「服务/聚合边界泄漏」的架构护栏。多服务共写共读同一批表，是 §2-§4 一堆字段/序列化/ID 漂移集成 bug 的根因。

## 资源 → 归属服务

| 资源 | 归属服务（唯一写权） | 其它服务的访问方式 |
|---|---|---|
| `app` / `api_key` | **auth** | 调 auth API（`/v1/apps`、`/v1/apps/{id}/api-keys`、`/v1/apikey/verify`）；portal 转发用户 JWT |
| `audit_log` / `audit_events` | **admin** | 调 admin API；`admin_db_session` 内部写审计（R0a） |
| `api` / `api_version` | **api-registry** | 调 api-registry API；发布走控制面 |
| `subscription` / `billing_record` | **billing** | 调 billing API |
| `plan` | **billing**（只读可共享） | 只读 |
| quota 计数（Redis `t:{tenant}:...`） | **quota** | 调 quota API |
| 调用日志（ClickHouse） | **trace-svc** 只读聚合 | 通过 trace-svc 查询；CH 无 RLS，强制 tenant 过滤（R3c） |
| `tenant` / `user` 身份 | **auth**（+ tenant-svc 元数据） | 调 auth/tenant API |

## 已修（R0c，2026-07-16）

- portal-bff 的 app/key 自助改走 auth API（`portal/routes.py` 转发，`portal/repository.py` 不再触达 `app`/`api_key` 表）。

## 待推进（按本表硬规则）

- admin 直写 `audit` → 改走 admin 自身 API（后续轮次）。
- quota / billing 从 ClickHouse 读用量算钱 → 明确为 trace-svc 只读聚合的消费者，不直连 CH 写状态。
- 多 Region 写亲和（ADR-013）需尊重本表的区域写权。
```

- [ ] **Step 2: `CLAUDE.md` Architecture 节加引用**

在 `CLAUDE.md` 的 `## Architecture` 节内（「Multitenancy + RLS」小节之后或「Task model」之后）追加一行：

```markdown
- **聚合所有权**：每个表/资源有唯一拥有服务，BFF 不得直写领域表——见 `docs/aggregate-ownership.md`（§9-B 护栏）。
```

- [ ] **Step 3: Commit**

```bash
git add docs/aggregate-ownership.md CLAUDE.md
git commit -m "R0c §3: 聚合所有权硬规则文档 + CLAUDE.md 引用"
```

---

## Task 4: 全量回归 + lint 收口

**Files:** 无（仅验证）。

- [ ] **Step 1: auth + portal 全量测试**

Run: `pytest services/services/auth/tests/ services/services/portal/tests/ -v`
Expected: 全 PASS。

- [ ] **Step 2: 全仓 lint**

Run: `ruff check services/`
Expected: 无 error。

- [ ] **Step 3: （可选，需 dev 栈）端到端打通**

前提：`make dev-up` 起 PG/Redis/auth/portal/dispatcher。然后：
1. 注册+登录外部开发者拿 JWT（`POST /v1/portal/auth/register` → verify → `POST /v1/portal/auth/login`）。
2. `POST /v1/portal/apps`（带 `Authorization: Bearer <jwt>`）→ 201，得 `app_id`。
3. `POST /v1/portal/apps/{app_id}/api-keys` → 201，得明文 `api_key`。
4. 用该 key 调任意受保护端点（或直接 `POST` auth `/v1/apikey/verify`）→ 200，`app_id`/`tenant_id` 正确（key 真实可用，链路打通）。
5. DB 抽查：`SELECT id, tenant_id FROM app WHERE id='<app_id>';` 命中且 tenant 正确。

> 跳过条件：无 dev 栈时不强制，单测已覆盖行为契约。skip 须在 PR 描述注明。

- [ ] **Step 4: PR-ready 自检**

Run: `git log --oneline main..HEAD`
Expected: 4 条 R0c commit（Task1-3 各一，Task4 无新 commit 除非补改）+ 先前 spec commit。
确认：工作区干净、`main..HEAD` 仅含 R0c 改动、未 push（等用户要求再 push 并 squash-merge）。

---

## Self-Review（plan 作者自查，已做）

- **Spec 覆盖**：spec ①auth 补端点 = Task 1；②portal 转发+删直写+字段映射 = Task 2；③owner 文档+CLAUDE 引用 = Task 3；验证（grep/auth/portal 测试/e2e）= Task 2 Step6 + Task 4。✅ 无遗漏。
- **占位符扫描**：无 TBD/TODO；每步含完整代码与命令。✅
- **类型/命名一致性**：repo `create_app`/`list_apps_for_tenant` ↔ route `create_app_route`/`list_apps_route`（防递归）；portal `ApiKeyResponse.key_prefix` ↔ auth `display_prefix` 映射在 Task1 测试与 Task2 测试+handler 三处一致。✅
- **风险点已落地步骤**：命名自调用 → Task1 Step5c 注释；unused import → Task2 Step4b + Step7 lint；portal 测试改造 → Task2 Step1 整段重写。✅
