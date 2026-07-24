# Admin/Portal 审计收尾（A3 编辑/删除 + P5 + P4）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 闭合 #84 审计清单剩余前端项——admin Apis 编辑/删除（A3）、portal Apps key revoke/rotate/前缀（P5）、admin+portal CSP（P4），合并到一个 squash-PR（续用 `feat/admin-audit-archive-apis-paging`，PR 范围 = A3 全量 + A4 + P5 + P4）。

**Architecture:** A3 = api-registry 直连 `PATCH`/`DELETE /v1/apis/{id}`（RLS + platform_admin 旁路，护栏硬删）。P5 = auth `ApiKeyListItem` 加 `signing`（派生自 `hmac_secret_encrypted IS NOT NULL`）+ portal-bff 3 个薄转发 + portal Apps 展开行 key 管理。P4 = admin/portal `index.html` CSP `<meta>` + 评估文档（cookie 迁移 defer）。base = `origin/main`。

**Tech Stack:** Python 3.11 / FastAPI / asyncpg（apihub_core.db_session RLS）/ pydantic 2；React 19 + TS + Ant Design Pro Components + Vite。测试 pytest asyncio_mode=auto。

## Global Constraints

- **分支**：`feat/admin-audit-archive-apis-paging`（已含 commit `8826911` A3 分页 + A4 归档 + `139a357` spec）。所有新 commit 叠在该分支上。
- **RLS 不破**：所有 PG 读写走 `db_session()`（事务内 `SET LOCAL app.tenant_id` + `app.is_platform_admin`）；不用 `admin_db_session`（仅 auth cross-tenant verify 用，本计划不动）。
- **SQL 安全**：`PATCH` 的 SET 子句列名来自 pydantic 模型固定字段集（非用户输入），值用 `$N` 参数化——`f-string` 只拼列名，**不拼值**。
- **ruff/mypy**：用 CI 钉版 `ruff==0.6.*`（本地 `/home/applo/.local/bin/ruff` 是 0.15.21，UP038/format 不一致）。实现后 `pip3 install --target /tmp/ruff06 'ruff==0.6.*' && /tmp/ruff06/bin/ruff check --fix && /tmp/ruff06/bin/ruff format` 对齐 CI。
- **前端**：admin + portal 各自 `npm run build`（含 tsc）双绿。不引新依赖。
- **每 task**：TDD（先红后绿）+ 独立 commit + 自验。push/merge 仅在用户要求时。
- **GateGuard**：每文件首编辑会被拦——陈述事实（哪文件、改什么、为何）后重试，或本会话 `ECC_GATEGUARD=off`。

---

## File Structure

**后端**
- `services/services/api-registry/src/api_registry/models.py` — 加 `ApiUpdate`（PATCH 请求体，`extra='forbid'`）。
- `services/services/api-registry/src/api_registry/routes.py` — 加 `PATCH`/`DELETE /v1/apis/{api_id}`（`get_api` 之后、`create_version` 之前）。
- `services/services/api-registry/tests/test_api_update_delete.py` — 新建，stub_db 假 conn。
- `services/services/auth/src/auth/models.py` — `ApiKeyListItem` 加 `signing: bool`。
- `services/services/auth/src/auth/repository.py` — `list_api_keys_for_app` SELECT 加派生列。
- `services/services/auth/tests/test_hmac_routes.py` — 加 list-keys 返 signing 的测试。
- `services/services/portal/src/portal/routes.py` — 加 3 个转发（list/revoke/rotate）。
- `services/services/portal/tests/test_routes.py` — 加 3 转发测试。

**前端**
- `frontend/admin/src/api/client.ts` — `api` 加 `patch`；`RequestOptions.method` 联合加 `'PATCH'`。
- `frontend/admin/src/pages/Apis.tsx` — 编辑 ModalForm + 删除 Popconfirm + 操作列按钮。
- `frontend/portal/src/pages/Apps.tsx` — `AppKeys` 展开行（列表 + 吊销 + 轮换 + 前缀）。
- `frontend/admin/index.html` + `frontend/portal/index.html` — CSP `<meta>`。

**文档**
- `docs/security-csp-eval.md` — 新建。

---

## Task 1: A3 后端 — api-registry PATCH/DELETE + ApiUpdate

**Files:**
- Modify: `services/services/api-registry/src/api_registry/models.py`
- Modify: `services/services/api-registry/src/api_registry/routes.py`（在 `get_api`（~L73）之后插入）
- Create: `services/services/api-registry/tests/test_api_update_delete.py`

**Interfaces:**
- Consumes: `apihub_core.db.db_session`、`apihub_core.tenant.require_tenant`、`apihub_core.kafka.emit`、`apihub_core.errors.{ApiError, ErrorCode}`。
- Produces: `PATCH /v1/apis/{api_id}`（body `ApiUpdate` → 返回更新后整行 dict）、`DELETE /v1/apis/{api_id}`（→ `{id, status:"deleted"}`，护栏 409）。后续 Task 2 前端调用此契约。

- [ ] **Step 1: 写 `ApiUpdate` 模型（先加，使路由可引用）**

`models.py` 顶部 import 改：
```python
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field
```
在 `ApiCreate` 类之后新增：
```python
class ApiUpdate(BaseModel):
    """PATCH /v1/apis/{id} —— 部分更新。

    base_path 不可变（不在字段集里）；model_config extra='forbid' → 调用方传
    任何额外字段（含 base_path）直接 422。
    """

    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=2, max_length=64)
    description: str | None = None
    category: str | None = Field(default=None, max_length=32)
    tags: list[str] | None = None
    visibility: Literal["private", "tenant", "public"] | None = None
```

- [ ] **Step 2: 写失败测试 `test_api_update_delete.py`**

```python
"""A3: PATCH/DELETE /v1/apis/{id} —— stub_db 假 conn，覆盖护栏与幂等。"""

import pytest
from contextlib import asynccontextmanager


@pytest.fixture
def stub_api_db(monkeypatch):
    """假 conn：跟踪 apis(id→row) + versions([{api_id,status}])，匹配 PATCH/DELETE 的 SQL 模式。"""
    apis: dict[str, dict] = {}
    versions: list[dict] = []

    class _Conn:
        async def fetchval(self, sql, *args):
            if "EXISTS" in sql and "api_version" in sql:
                api_id = args[0]
                return any(
                    v["api_id"] == api_id and v["status"] in ("published", "deprecated", "reviewing")
                    for v in versions
                )
            return None

        async def fetchrow(self, sql, *args):
            if sql.startswith("UPDATE api SET"):
                api_id = args[0]
                row = apis.get(api_id)
                if row is None:
                    return None
                set_part = sql.split("SET ", 1)[1].split(", updated_at", 1)[0]
                for pair in set_part.split(","):
                    col, _, idx = pair.strip().partition(" = ")
                    n = int(idx.replace("$", ""))
                    row[col] = args[n - 1]
                return dict(row)
            if sql.startswith("SELECT * FROM api WHERE id"):
                return dict(apis.get(args[0])) if args[0] in apis else None
            return None

        async def execute(self, sql, *args):
            if "DELETE FROM api_version" in sql:
                api_id = args[0]
                before = len(versions)
                versions[:] = [v for v in versions if v["api_id"] != api_id]
                return f"DELETE {before - len(versions)}"
            if "DELETE FROM api" in sql:
                return "DELETE 1" if args[0] in apis else "DELETE 0"
            return "UPDATE 1"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    @asynccontextmanager
    async def _fake_session():
        yield _Conn()

    from apihub_core import db as db_mod

    monkeypatch.setattr(db_mod, "db_session", _fake_session)

    def _seed(api_id, **fields):
        apis[api_id] = {
            "id": api_id, "tenant_id": "42", "name": "n", "description": None,
            "category": "c", "base_path": "/x", "tags": [], "status": "draft",
            "visibility": "private", **fields,
        }

    return {
        "seed": _seed,
        "add_version": lambda api_id, status: versions.append({"api_id": api_id, "status": status}),
    }


pytestmark = pytest.mark.asyncio


async def test_patch_updates_fields(admin_client, stub_api_db, stub_kafka):
    stub_api_db["seed"]("api_1", name="old", description="d", category="c", tags=["a"])
    r = await admin_client.patch("/v1/apis/api_1", json={"name": "new", "tags": ["x", "y"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "new"
    assert body["tags"] == ["x", "y"]
    assert any(p[1]["action"] == "api.update" for p in stub_kafka)


async def test_patch_rejects_base_path(admin_client, stub_api_db):
    stub_api_db["seed"]("api_1")
    r = await admin_client.patch("/v1/apis/api_1", json={"base_path": "/changed"})
    assert r.status_code == 422


async def test_patch_unknown_api_404(admin_client, stub_api_db):
    r = await admin_client.patch("/v1/apis/nope", json={"name": "x"})
    assert r.status_code == 404


async def test_patch_empty_body_is_idempotent(admin_client, stub_api_db):
    stub_api_db["seed"]("api_1", name="keep")
    r = await admin_client.patch("/v1/apis/api_1", json={})
    assert r.status_code == 200
    assert r.json()["name"] == "keep"


async def test_delete_blocked_when_published_version(admin_client, stub_api_db):
    stub_api_db["seed"]("api_1")
    stub_api_db["add_version"]("api_1", "published")
    r = await admin_client.delete("/v1/apis/api_1")
    assert r.status_code == 409


async def test_delete_cascades_when_only_draft_retired(admin_client, stub_api_db):
    stub_api_db["seed"]("api_1")
    stub_api_db["add_version"]("api_1", "draft")
    stub_api_db["add_version"]("api_1", "retired")
    r = await admin_client.delete("/v1/apis/api_1")
    assert r.status_code == 200, r.text
    assert r.json() == {"id": "api_1", "status": "deleted"}


async def test_delete_unknown_api_404(admin_client, stub_api_db):
    r = await admin_client.delete("/v1/apis/nope")
    assert r.status_code == 404
```

> 注：`admin_client`/`stub_kafka` 来自 `tests/conftest.py`。`stub_api_db` 覆盖 `db.db_session`，与 `stub_kafka`（覆盖 `kafka.emit`）正交。

- [ ] **Step 3: 跑测试确认失败**

Run: `cd services/services/api-registry && PYTHONPATH=src pytest tests/test_api_update_delete.py -v`
Expected: FAIL（路由未定义 → 405/404）。

- [ ] **Step 4: 实现路由 `routes.py`**

顶部 import 改（加 `ApiUpdate`）：
```python
from api_registry.models import (
    ApiCreate,
    ApiUpdate,
    ApiVersionCreate,
    ApiVersionResponse,
)
```
在 `get_api` 函数之后、`create_version` 之前插入：
```python
    @app.patch("/v1/apis/{api_id}")
    async def update_api(api_id: str, payload: ApiUpdate):
        """部分更新 API 元数据。base_path 不可变（payload 不含该字段；传则 pydantic 422）。"""
        require_tenant()
        updates = {
            k: v for k, v in payload.model_dump(exclude_unset=True).items() if v is not None
        }
        async with db.db_session() as conn:
            if not updates:
                row = await conn.fetchrow("SELECT * FROM api WHERE id = $1", api_id)
            else:
                # 列名来自模型固定字段集（非用户输入），值参数化 —— 安全
                set_clauses = ", ".join(f"{c} = ${i + 2}" for i, c in enumerate(updates))
                row = await conn.fetchrow(
                    f"UPDATE api SET {set_clauses}, updated_at = NOW() WHERE id = $1 RETURNING *",
                    api_id,
                    *updates.values(),
                )
        if not row:
            raise ApiError(ErrorCode.API_NOT_FOUND, f"API {api_id} not found")
        if updates:
            await kafka.emit(
                "audit-events",
                {
                    "action": "api.update",
                    "resource_type": "api",
                    "resource_id": api_id,
                    "detail": updates,
                },
            )
        return dict(row)

    @app.delete("/v1/apis/{api_id}")
    async def delete_api(api_id: str):
        """删除 API。护栏：存在 published/deprecated/reviewing 版本则 409；否则级联删版本+api。"""
        require_tenant()
        async with db.db_session() as conn:
            blocked = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM api_version "
                "WHERE api_id = $1 AND status IN ('published','deprecated','reviewing'))",
                api_id,
            )
            if blocked:
                raise ApiError(
                    ErrorCode.CONFLICT,
                    "API has active versions (published/deprecated/reviewing); retire them first",
                    http_status=409,
                )
            await conn.execute("DELETE FROM api_version WHERE api_id = $1", api_id)
            result = await conn.execute("DELETE FROM api WHERE id = $1", api_id)
        if not result.endswith(" 1"):
            raise ApiError(ErrorCode.API_NOT_FOUND, f"API {api_id} not found")
        await kafka.emit(
            "audit-events",
            {"action": "api.delete", "resource_type": "api", "resource_id": api_id},
        )
        return {"id": api_id, "status": "deleted"}
```

- [ ] **Step 5: 跑测试确认通过 + 回归**

Run: `cd services/services/api-registry && PYTHONPATH=src pytest tests/ -v`
Expected: 全绿（7 新 + lifecycle/change_request 不回归）。

- [ ] **Step 6: lint**

Run: `pip3 install --target /tmp/ruff06 'ruff==0.6.*'` （若不存在）；`/tmp/ruff06/bin/ruff check --fix services/services/api-registry && /tmp/ruff06/bin/ruff format services/services/api-registry`
Expected: 0 errors。

- [ ] **Step 7: commit**

```bash
git add services/services/api-registry/src/api_registry/models.py \
        services/services/api-registry/src/api_registry/routes.py \
        services/services/api-registry/tests/test_api_update_delete.py
git commit -m "feat(api-registry): A3 PATCH/DELETE /v1/apis/{id}（护栏硬删）"
```

---

## Task 2: A3 前端 — admin Apis 编辑/删除 + client.patch

**Files:**
- Modify: `frontend/admin/src/api/client.ts`
- Modify: `frontend/admin/src/pages/Apis.tsx`
- Verify: `frontend/admin/src/api/types.ts`（`ApiListItem` 字段）
- Test: `cd frontend/admin && npm run build`（含 tsc）

**Interfaces:**
- Consumes: Task 1 的 `PATCH/DELETE /v1/apis/{id}`。
- Produces: 编辑 Modal（name/description/category/tags/visibility，base_path 只读）+ 删除二次确认。

- [ ] **Step 1: client.ts 加 `patch`**

把 `RequestOptions.method` 联合改为：
```ts
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';
```
`api` 对象加一行（`put` 之后、`del` 之前）：
```ts
  patch: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'PATCH', body }),
```

- [ ] **Step 2: types.ts 确保 `ApiListItem` 字段齐全**

Run: `grep -n "ApiListItem" -A 12 frontend/admin/src/api/types.ts`
若缺 `description` / `tags` / `visibility`，在 `ApiListItem` 内补：
```ts
  description?: string | null;
  tags?: string[] | null;
  visibility?: string;
```

- [ ] **Step 3: Apis.tsx —— 加编辑 state + 操作列按钮**

顶部 `@ant-design/pro-components` import 加 `ProFormSelect`（若未导入）。`Apis` 组件内 `const [createOpen, setCreateOpen] = useState(false);` 之后加：
```ts
  const [editTarget, setEditTarget] = useState<ApiListItem | null>(null);
```
把操作列（`title: '操作'` 的 `render`）替换为：
```tsx
      {
        title: '操作',
        width: 200,
        fixed: 'right',
        render: (_, r) => (
          <Space>
            <Button size="small" icon={<EyeOutlined />} onClick={() => setDrawerId(r.id)}>
              详情
            </Button>
            <Button size="small" onClick={() => setEditTarget(r)}>
              编辑
            </Button>
            <Popconfirm
              title="删除该 API？"
              description="若有 published/deprecated 版本将被拒绝；draft/retired 版本会一并删除。"
              okText="删除"
              okButtonProps={{ danger: true }}
              onConfirm={async () => {
                try {
                  await api.del(`${REGISTRY}/apis/${r.id}`);
                  message.success('已删除');
                  void load();
                } catch (e) {
                  const err = e as { status?: number; message?: string };
                  if (err.status === 409) {
                    message.warning('存在 published/deprecated/reviewing 版本，请先下线');
                  } else {
                    message.error(err.message ?? '删除失败');
                  }
                }
              }}
            >
              <Button size="small" danger>
                删除
              </Button>
            </Popconfirm>
          </Space>
        ),
      },
```
（`Popconfirm`、`Space` 已在文件顶部 antd import。）

- [ ] **Step 4: Apis.tsx —— 加编辑 ModalForm**

在 `Apis` 组件 return 中，`<ApiDrawer ... />` 之后插入：
```tsx
      <ModalForm
        title="编辑 API"
        width={560}
        open={editTarget !== null}
        onOpenChange={(open) => { if (!open) setEditTarget(null); }}
        modalProps={{ destroyOnClose: true }}
        initialValues={
          editTarget
            ? {
                name: editTarget.name,
                description: editTarget.description,
                category: editTarget.category,
                tags: (editTarget.tags ?? []).join(','),
                visibility: editTarget.visibility,
                base_path: editTarget.base_path,
              }
            : {}
        }
        onFinish={async (values) => {
          if (!editTarget) return false;
          const body: Record<string, unknown> = {};
          if (values.name != null) body.name = values.name;
          if (values.description != null) body.description = values.description;
          if (values.category != null) body.category = values.category;
          if (values.tags != null)
            body.tags = String(values.tags).split(',').map((s) => s.trim()).filter(Boolean);
          if (values.visibility != null) body.visibility = values.visibility;
          try {
            await api.patch(`${REGISTRY}/apis/${editTarget.id}`, body);
            message.success('已保存');
            void load();
            return true;
          } catch (e) {
            message.error((e as Error).message);
            return false;
          }
        }}
      >
        <ProFormText name="name" label="名称" />
        <ProFormText name="description" label="描述" />
        <ProFormText name="category" label="分类" />
        <ProFormText name="tags" label="标签（逗号分隔）" />
        <ProFormSelect
          name="visibility"
          label="可见性"
          options={[
            { label: 'private', value: 'private' },
            { label: 'tenant', value: 'tenant' },
            { label: 'public', value: 'public' },
          ]}
        />
        <ProFormText name="base_path" label="base_path（不可改）" disabled />
      </ModalForm>
```

- [ ] **Step 5: typecheck + build**

Run: `cd frontend/admin && npm run build`
Expected: 双绿。

- [ ] **Step 6: commit**

```bash
git add frontend/admin/src/api/client.ts frontend/admin/src/api/types.ts frontend/admin/src/pages/Apis.tsx
git commit -m "feat(admin): A3 Apis 编辑/删除 UI + client.patch"
```

---

## Task 3: P5 后端 — auth ApiKeyListItem.signing

**Files:**
- Modify: `services/services/auth/src/auth/models.py`（`ApiKeyListItem` ~L65）
- Modify: `services/services/auth/src/auth/repository.py:150`（`list_api_keys_for_app` SELECT）
- Modify: `services/services/auth/tests/test_hmac_routes.py`（加 list-keys 测试）

**Interfaces:**
- Consumes: `api_key.hmac_secret_encrypted` 列（R2e）。
- Produces: `GET /v1/apps/{app_id}/api-keys` 响应每项多 `signing: bool`（Task 4 BFF 透传，Task 5 前端据此 gate rotate）。

- [ ] **Step 1: 写失败测试（加到 `test_hmac_routes.py` 末尾）**

```python
# ========== GET /v1/apps/{app_id}/api-keys —— signing 字段 ==========


async def test_list_keys_includes_signing_flag(client, authed, monkeypatch):
    """list 端点透传派生 signing（hmac_secret_encrypted IS NOT NULL）。"""
    from auth import repository as repo

    async def _fake_list(app_id):  # noqa: ARG001
        return [
            {
                "id": "key_plain", "app_id": "app_x", "name": "plain", "scopes": [],
                "display_prefix": "ak_plain", "status": "active", "last_used_at": None,
                "expires_at": None, "created_at": "2026-07-16T00:00:00", "revoked_at": None,
                "signing": False,
            },
            {
                "id": "key_sign", "app_id": "app_x", "name": "sign", "scopes": [],
                "display_prefix": "ak_sign", "status": "active", "last_used_at": None,
                "expires_at": None, "created_at": "2026-07-16T00:00:00", "revoked_at": None,
                "signing": True,
            },
        ]

    monkeypatch.setattr(repo, "list_api_keys_for_app", _fake_list)
    r = await client.get("/v1/apps/app_x/api-keys")
    assert r.status_code == 200, r.text
    items = r.json()
    by_id = {i["id"]: i for i in items}
    assert by_id["key_plain"]["signing"] is False
    assert by_id["key_sign"]["signing"] is True
```

- [ ] **Step 2: 跑确认失败**

Run: `cd services/services/auth && PYTHONPATH=src pytest tests/test_hmac_routes.py::test_list_keys_includes_signing_flag -v`
Expected: FAIL（`ApiKeyListItem` 无 `signing` → 500/校验错）。

- [ ] **Step 3: models.py 加字段**

`ApiKeyListItem` 末尾（`revoked_at` 之后）加：
```python
    signing: bool = False  # = hmac_secret_encrypted IS NOT NULL；前端据此 gate rotate
```

- [ ] **Step 4: repository.py SELECT 加派生列**

`list_api_keys_for_app` 的 SQL（`key_prefix AS display_prefix,` 之后、`status` 之前）加：
```python
            SELECT id, app_id, name, scopes, key_prefix AS display_prefix,
                   (hmac_secret_encrypted IS NOT NULL) AS signing,
                   status, last_used_at, expires_at, created_at, revoked_at
            FROM api_key
            WHERE app_id = $1
            ORDER BY created_at DESC
```

- [ ] **Step 5: 跑确认通过 + 回归**

Run: `cd services/services/auth && PYTHONPATH=src pytest tests/test_hmac_routes.py -v`
Expected: 全绿。

- [ ] **Step 6: lint + commit**

```bash
/tmp/ruff06/bin/ruff check --fix services/services/auth && /tmp/ruff06/bin/ruff format services/services/auth
git add services/services/auth/src/auth/models.py \
        services/services/auth/src/auth/repository.py \
        services/services/auth/tests/test_hmac_routes.py
git commit -m "feat(auth): P5 ApiKeyListItem 加 signing（gate rotate）"
```

---

## Task 4: P5 BFF — portal 3 转发

**Files:**
- Modify: `services/services/portal/src/portal/routes.py`（`create_api_key` 之后）
- Modify: `services/services/portal/tests/test_routes.py`（加 3 测试）

**Interfaces:**
- Consumes: auth `GET /v1/apps/{app_id}/api-keys`、`DELETE /v1/api-keys/{key_id}`、`POST /v1/api-keys/{key_id}/hmac-secret/rotate`（Task 3 的 `signing` 透传）。
- Produces: `GET/DELETE/POST /v1/portal/...`（Task 5 前端调用）。

- [ ] **Step 1: 写 3 个失败测试（加到 `test_routes.py` 的 `test_create_api_key_forwards_and_maps_prefix` 之后）**

```python
async def test_list_api_keys_forwards_to_auth(client, monkeypatch):
    import httpx as _httpx
    captured = {}

    class _FakeResp:
        status_code = 200
        def json(self):
            return [{"id": "key_1", "app_id": "app_x", "name": "k", "scopes": [],
                     "display_prefix": "ak_ab", "status": "active", "last_used_at": None,
                     "expires_at": None, "created_at": "2026-07-16T00:00:00",
                     "revoked_at": None, "signing": True}]

    class _FakeClient:
        def __init__(self, *a, **kw): pass
        async def request(self, method, url, **kw):
            captured["method"] = method
            captured["url"] = url
            return _FakeResp()
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return False

    monkeypatch.setattr(_httpx, "AsyncClient", _FakeClient)
    r = await client.get("/v1/portal/apps/app_x/api-keys")
    assert r.status_code == 200
    assert r.json()[0]["signing"] is True
    assert captured["method"] == "GET"
    assert captured["url"] == "http://auth.apihub-system/v1/apps/app_x/api-keys"


async def test_revoke_api_key_forwards_to_auth(client, monkeypatch):
    import httpx as _httpx
    captured = {}

    class _FakeResp:
        status_code = 200
        def json(self): return {"id": "key_1", "status": "revoked"}

    class _FakeClient:
        def __init__(self, *a, **kw): pass
        async def request(self, method, url, **kw):
            captured["method"] = method
            captured["url"] = url
            return _FakeResp()
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return False

    monkeypatch.setattr(_httpx, "AsyncClient", _FakeClient)
    r = await client.delete("/v1/portal/api-keys/key_1")
    assert r.status_code == 200
    assert captured["method"] == "DELETE"
    assert captured["url"] == "http://auth.apihub-system/v1/api-keys/key_1"


async def test_rotate_api_key_forwards_to_auth(client, monkeypatch):
    import httpx as _httpx
    captured = {}

    class _FakeResp:
        status_code = 200
        def json(self): return {"key_id": "key_1", "hmac_secret": "new_secret_xyz"}

    class _FakeClient:
        def __init__(self, *a, **kw): pass
        async def request(self, method, url, **kw):
            captured["method"] = method
            captured["url"] = url
            return _FakeResp()
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return False

    monkeypatch.setattr(_httpx, "AsyncClient", _FakeClient)
    r = await client.post("/v1/portal/api-keys/key_1/hmac-secret/rotate")
    assert r.status_code == 200
    assert r.json()["hmac_secret"] == "new_secret_xyz"
    assert captured["method"] == "POST"
    assert captured["url"] == "http://auth.apihub-system/v1/api-keys/key_1/hmac-secret/rotate"
```

- [ ] **Step 2: 跑确认失败**

Run: `cd services/services/portal && PYTHONPATH=src pytest tests/test_routes.py -k "list_api_keys or revoke_api_key or rotate_api_key" -v`
Expected: 3 FAIL（404 未定义）。

- [ ] **Step 3: 实现路由（`create_api_key` 之后插入）**

```python
    @app.get("/v1/portal/apps/{app_id}/api-keys")
    async def list_api_keys(request: Request, app_id: str):
        """列 app 的 APIKey —— 透传 auth（含 signing 派生字段）。"""
        require_tenant()
        st, body = await _forward(
            "GET",
            f"/v1/apps/{app_id}/api-keys",
            headers={"Authorization": request.headers.get("Authorization", "")},
        )
        if st >= 400:
            raise ApiError(ErrorCode.INTERNAL, f"auth error: {body}", http_status=st)
        return body

    @app.delete("/v1/portal/api-keys/{key_id}")
    async def revoke_api_key(request: Request, key_id: str):
        """吊销 APIKey —— 转发 auth。"""
        require_tenant()
        st, body = await _forward(
            "DELETE",
            f"/v1/api-keys/{key_id}",
            headers={"Authorization": request.headers.get("Authorization", "")},
        )
        if st >= 400:
            raise ApiError(ErrorCode.INTERNAL, f"auth error: {body}", http_status=st)
        return body

    @app.post("/v1/portal/api-keys/{key_id}/hmac-secret/rotate")
    async def rotate_api_key(request: Request, key_id: str):
        """轮换 HMAC secret —— 转发 auth，新明文仅此次返回。"""
        require_tenant()
        st, body = await _forward(
            "POST",
            f"/v1/api-keys/{key_id}/hmac-secret/rotate",
            headers={"Authorization": request.headers.get("Authorization", "")},
        )
        if st >= 400:
            raise ApiError(ErrorCode.INTERNAL, f"auth error: {body}", http_status=st)
        return body
```

- [ ] **Step 4: 跑确认通过 + 回归**

Run: `cd services/services/portal && PYTHONPATH=src pytest tests/test_routes.py -v`
Expected: 全绿（3 新 + 原 16 不回归）。

- [ ] **Step 5: lint + commit**

```bash
/tmp/ruff06/bin/ruff check --fix services/services/portal && /tmp/ruff06/bin/ruff format services/services/portal
git add services/services/portal/src/portal/routes.py services/services/portal/tests/test_routes.py
git commit -m "feat(portal): P5 BFF 转发 list/revoke/rotate api-keys"
```

---

## Task 5: P5 前端 — portal Apps key 管理（列表/吊销/轮换/前缀）

**Files:**
- Modify: `frontend/portal/src/pages/Apps.tsx`
- Test: `cd frontend/portal && npm run build`

**Interfaces:**
- Consumes: Task 4 的 `GET/DELETE/POST /v1/portal/...`。
- Produces: 每个 app 展开行显示 key 列表（前缀+name+status+created+last_used）+ 吊销 + 轮换（仅 signing key）。

- [ ] **Step 1: imports + 类型**

文件顶部首行改（含 `useEffect`）：
```ts
import { useEffect, useRef, useState } from 'react';
```
antd import 加 `Alert`, `Modal`, `Table`（若未导入，补到现有 antd import 块）。加 `import dayjs from 'dayjs';` 与 `import type { ColumnsType } from 'antd/es/table';`。
文件内（`App` interface 之后）加：
```ts
interface AppKey {
  id: string;
  name: string;
  display_prefix: string;
  status: string;
  signing: boolean;
  created_at: string;
  last_used_at: string | null;
}
```

- [ ] **Step 2: 加 `AppKeys` 子组件（文件底部，`Apps` 函数之后）**

```tsx
function AppKeys({ appId }: { appId: string }) {
  const [keys, setKeys] = useState<AppKey[]>([]);
  const [loading, setLoading] = useState(false);
  const [rotated, setRotated] = useState<string | null>(null);

  const reload = async () => {
    setLoading(true);
    try {
      const r = await api.get<AppKey[]>(`/v1/portal/apps/${appId}/api-keys`);
      setKeys(r);
    } catch (e) {
      message.error((e as Error).message);
      setKeys([]);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void reload();
  }, [appId]);

  const revoke = async (keyId: string) => {
    try {
      await api.del(`/v1/portal/api-keys/${keyId}`);
      message.success('已吊销');
      void reload();
    } catch (e) {
      message.error((e as Error).message);
    }
  };

  const rotate = async (keyId: string) => {
    try {
      const r = await api.post<{ hmac_secret: string }>(
        `/v1/portal/api-keys/${keyId}/hmac-secret/rotate`,
      );
      setRotated(r.hmac_secret);
    } catch (e) {
      message.error((e as Error).message);
    }
  };

  const columns: ColumnsType<AppKey> = [
    { title: '前缀', dataIndex: 'display_prefix', render: (v) => <Typography.Text code>{v}…</Typography.Text> },
    { title: '名称', dataIndex: 'name' },
    { title: '状态', dataIndex: 'status', width: 100, render: (v) => <Tag color={v === 'active' ? 'green' : 'default'}>{v}</Tag> },
    { title: '创建', dataIndex: 'created_at', width: 150, render: (v) => dayjs(v).format('MM-DD HH:mm') },
    { title: '最后使用', dataIndex: 'last_used_at', width: 150, render: (v) => (v ? dayjs(v).format('MM-DD HH:mm') : '—') },
    {
      title: '操作',
      width: 160,
      render: (_, r) => (
        <Space>
          {r.status === 'active' && (
            <Popconfirm title="吊销该 Key？不可恢复。" okText="吊销" okButtonProps={{ danger: true }} onConfirm={() => void revoke(r.id)}>
              <Button size="small" danger>吊销</Button>
            </Popconfirm>
          )}
          {r.signing && r.status === 'active' && (
            <Popconfirm title="轮换 HMAC secret？旧 secret 立即失效。" okText="轮换" onConfirm={() => void rotate(r.id)}>
              <Button size="small">轮换</Button>
            </Popconfirm>
          )}
        </Space>
      ),
    },
  ];

  return (
    <>
      <Table<AppKey> rowKey="id" size="small" columns={columns} dataSource={keys} loading={loading} pagination={false} />
      <Modal
        open={rotated !== null}
        title="新 HMAC Secret（仅显示一次）"
        okText="我已复制保存"
        cancelText="关闭"
        onOk={() => setRotated(null)}
        onCancel={() => setRotated(null)}
      >
        <Alert
          type="warning"
          showIcon
          message="请立即复制保存，旧 secret 已失效"
          description={
            <Typography.Text code copyable style={{ wordBreak: 'break-all', display: 'block' }}>
              {rotated ?? ''}
            </Typography.Text>
          }
        />
      </Modal>
    </>
  );
}
```

- [ ] **Step 3: `Apps` 的 ProTable 加 `expandable`**

在 `<ProTable<App>` 上加（与 `columns=` 同级）：
```tsx
        expandable={{
          expandedRowRender: (r) => <AppKeys appId={r.id} />,
          rowExpandable: () => true,
        }}
```

- [ ] **Step 4: typecheck + build**

Run: `cd frontend/portal && npm run build`
Expected: 双绿。

- [ ] **Step 5: commit**

```bash
git add frontend/portal/src/pages/Apps.tsx
git commit -m "feat(portal): P5 Apps key 管理（列表/吊销/轮换/前缀）"
```

---

## Task 6: P4 — CSP meta + 评估文档

**Files:**
- Modify: `frontend/admin/index.html`
- Modify: `frontend/portal/index.html`
- Create: `docs/security-csp-eval.md`
- Test: `npm run build` 两端 + grep 验证无 inline script

**Interfaces:**
- Consumes: 无（纯前端静态 + 文档）。
- Produces: 两个 SPA 的 CSP `<meta>` + 评估文档。

- [ ] **Step 1: admin index.html 加 CSP meta**

在 `<meta name="viewport" content="..." />` 之后插入：
```html
    <meta
      http-equiv="Content-Security-Policy"
      content="default-src 'self';
               script-src 'self';
               style-src 'self' 'unsafe-inline';
               img-src 'self' data:;
               font-src 'self' data:;
               connect-src 'self';
               object-src 'none';
               base-uri 'self';
               form-action 'self'"
    />
```

- [ ] **Step 2: portal index.html 同款 meta**

同样在 portal `<meta name="viewport" ... />` 之后插入（同上 meta 块）。

- [ ] **Step 3: 验证 prod build 无 inline script（script-src 'self' 成立的前提）**

Run: `cd frontend/admin && npm run build && grep -rn "<script" dist/ | grep -v 'src=' || echo "no inline script (ok)"`
Expected: `no inline script (ok)`。对 portal 同样跑一次。
> 若发现 inline `<script>`（Vite modulepreload polyfill 等）：在 `vite.config.ts` 加 `build: { modulePreload: { polyfill: false } }`（现代浏览器不需 polyfill），或为该 script 加 `'sha256-...'` 到 script-src。优先 polyfill:false。

- [ ] **Step 4: 写 `docs/security-csp-eval.md`**

```markdown
# Content-Security-Policy 评估（P4）

## 现状
admin 与 portal 均为 Vite 构建的 SPA，access/refresh JWT 存 `localStorage`（`apihub_admin_token` / `apihub_portal_token` 及对应 refresh）。admin-svc 是纯 BFF（无 StaticFiles），不伺服 SPA。

## 已落地（本轮）
admin/portal `index.html` 加 `<meta http-equiv="Content-Security-Policy">`：
- `script-src 'self'` —— 禁 inline/eval（仅外部同源脚本）。
- `style-src 'self' 'unsafe-inline'` —— AntD 运行时内联样式不可避免。
- `img-src/font-src 'self' data:` —— 允许 data-URI 图标/字体。
- `connect-src 'self'` —— 仅同源 API（`/api/*` 代理），阻断 XSS 外发 beacon。
- `object-src 'none'; base-uri 'self'; form-action 'self'`。

## 残余风险（接受）
CSP **降损不消除**：self-origin 的 stored XSS 仍可读 `localStorage` 里的 JWT 并经同源 `/api/*` 外泄。CSP 阻断的是 inline 注入与跨域外发，不能阻止同源 JS 读 localStorage。

## 迁移路径（defer）
httpOnly cookie 会话可根治 localStorage XSS 盗取：
- BFF 登录回 `Set-Cookie: access=...; Secure; HttpOnly; SameSite=Lax`（refresh 同款或 server-side session）。
- CSRF：double-submit token 或 SameSite=Strict。
- 前端去掉 `localStorage` 读写，凭证由浏览器自动附 cookie。
- CORS：`credentials: 'include'` + 白名单 origin。
属新架构，单列后续轮。

## follow-up（meta 无法设）
- `frame-ancestors 'none'` / `X-Frame-Options: DENY`：需在 SPA 伺服层（nginx/APISIX）以 header 设（meta 不支持 frame-ancestors）。
- 若 prod 中 SPA 与 API 不同源，`connect-src` 需加 API origin。
```

- [ ] **Step 5: commit**

```bash
git add frontend/admin/index.html frontend/portal/index.html docs/security-csp-eval.md
git commit -m "feat(security): P4 admin/portal CSP meta + 评估文档"
```

---

## 整轮收尾（全部 task 后）

- [ ] **全量 lint**：`/tmp/ruff06/bin/ruff check --fix services/ && /tmp/ruff06/bin/ruff format services/` → 0 errors。
- [ ] **全量 mypy**：`mypy services/` → 0 新错（r3l 后基线为 0）。
- [ ] **受影响服务 test**：api-registry / auth / portal 各 `PYTHONPATH=src pytest` → 全绿。
- [ ] **前端**：admin + portal `npm run build` 双绿。
- [ ] **更新 MEMORY**：`apihub-fix-program-progress.md` 加本轮条目（A3 ed/del + P5 + P4，续用分支）。
- [ ] **不 push/merge**——等用户要求时再 push 并开/合 PR（PR body 说明范围 = A3 全量 + A4 + P5 + P4）。
