# Phase 3 第二切片「API 目录 + 在线调试」Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让外部开发者在 Portal 中浏览 API 目录 + 搜索/过滤 + 查看详情 + 在线调试（用 API Key 调通真实后端）。

**Architecture:** portal-bff 扩展 3 个端点（API 列表/详情/try 代理），Portal 前端新增 2 页（API 目录 + 详情/try-it）。portal-bff 从 PG 读 API 元数据（RLS 隔离），try 端点通过 httpx 直接调后端 URL（不走 dispatcher 减少延迟）。

**Tech Stack:** Python FastAPI + asyncpg / React + TS + Vite + Tailwind CSS + Zustand

## Global Constraints

- `db_session()` 必须用于所有 PG 读取（RLS 自动隔离）
- API Key 只在服务端传递，不暴露给前端 JS 变量
- try 端点不抛 ApiError——所有异常捕获为 TryResponse.error，HTTP 永远 200
- `ruff check` + `mypy` clean before commit
- 新增模型放在 `portal/models.py`，数据访问放在 `portal/repository.py`
- Portal 前端复用现有 `api/client.ts`（JWT auth, Bearer token）
- 后端类型色标：HTTP=蓝色 / AI SSE=紫色 / Async Task=橙色 / Workflow=灰色
- try 端点的 backend_url 从 PG 直接读取，不经过 PortalVersionItem（避免暴露内部 URL 给前端）

---

### Task 1: portal-bff models — 新增 Pydantic 模型

**Files:**
- Modify: `services/services/portal/src/portal/models.py`

**Interfaces:**
- Consumes: nothing — standalone model definitions
- Produces: `PortalApiItem`, `PortalApiListResponse`, `PortalApiDetail`, `PortalVersionItem`, `TryRequest`, `TryResponse` — consumed by routes.py and repository.py

- [ ] **Step 1: Append new models to `models.py`**

```python
from typing import Any


class PortalApiItem(BaseModel):
    """API 目录列表项（Portal 公开字段，隐藏 backend_url 等内部信息）。"""
    api_id: str
    name: str
    description: str | None = None
    category: str = ""
    tags: list[str] = []
    base_path: str
    visibility: str = "public"
    backend_type: str = "http"
    version: str = ""
    updated_at: str = ""


class PortalApiListResponse(BaseModel):
    items: list[PortalApiItem]
    total: int
    limit: int
    offset: int
    categories: list[str] = []
    tags: list[str] = []


class PortalVersionItem(BaseModel):
    """API 版本详情（不含 backend_url——仅服务端知道）。"""
    version_id: str
    version: str
    method: str
    path: str
    backend_type: str = "http"
    status: str
    request_schema: dict[str, Any] | None = None
    response_schema: dict[str, Any] | None = None
    masking: dict[str, Any] | None = None
    ai_model: str | None = None
    ai_streaming: bool = False


class PortalApiDetail(BaseModel):
    api_id: str
    name: str
    description: str | None = None
    category: str = ""
    tags: list[str] = []
    base_path: str
    visibility: str = "public"
    api_status: str
    versions: list[PortalVersionItem] = []


class TryRequest(BaseModel):
    """在线调试请求体。api_key 只在服务端传递。"""
    api_id: str
    version_id: str | None = None   # 不传则用最新 published
    method: str = "GET"
    path_params: dict[str, str] = {}
    query_params: dict[str, str] = {}
    headers: dict[str, str] = {}
    body: Any = None                # JSON body
    api_key: str                    # 调用者的 API Key
    timeout_ms: int = 30000


class TryResponse(BaseModel):
    """在线调试响应。HTTP 永远 200，真实 status 在字段内。"""
    status: int
    headers: dict[str, str] = {}
    body: Any = None
    latency_ms: int = 0
    error: str | None = None
```

- [ ] **Step 2: Verify import**

```bash
cd services/services/portal
python -c "from portal.models import PortalApiItem, PortalApiDetail, TryRequest, TryResponse; print('OK')"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add services/services/portal/src/portal/models.py
git commit -m "feat(portal-models): API 目录 + 在线调试 Pydantic 模型"
```

---

### Task 2: portal-bff repository — 新增数据访问函数

**Files:**
- Modify: `services/services/portal/src/portal/repository.py`

**Interfaces:**
- Consumes: models from Task 1
- Produces: `list_portal_apis()`, `get_api_detail()`, `try_api()` — consumed by routes.py

- [ ] **Step 1: Append data access functions to `repository.py`**

```python
from typing import Any

import httpx
from apihub_core import db
from apihub_core.config import get_settings
from apihub_core.errors import ApiError, ErrorCode

from portal.models import (
    PortalApiDetail,
    PortalApiItem,
    PortalApiListResponse,
    PortalVersionItem,
    TryRequest,
    TryResponse,
)


async def list_portal_apis(
    search: str = "",
    category: str = "",
    tag: str = "",
    limit: int = 50,
    offset: int = 0,
) -> PortalApiListResponse:
    """API 目录列表 + 搜索/过滤/分页。
    
    通过 db_session (RLS) 自动按 caller 租户过滤可见 API。
    """
    search_clause = ""
    params: list[Any] = []
    idx = 1

    if search:
        search_clause = f" AND (a.name ILIKE ${idx} OR a.description ILIKE ${idx})"
        params.append(f"%{search}%")
        idx += 1
    if category:
        search_clause += f" AND a.category = ${idx}"
        params.append(category)
        idx += 1
    if tag:
        search_clause += f" AND ${idx} = ANY(a.tags)"
        params.append(tag)
        idx += 1

    async with db.db_session() as conn:
        total = await conn.fetchval(
            f"SELECT COUNT(*) FROM api a WHERE a.status = 'published'{search_clause}",
            *params,
        )

        list_sql = f"""
            SELECT a.id, a.name, a.description, a.category, a.tags,
                   a.base_path, a.visibility, v.backend_type, v.version, a.updated_at
            FROM api a
            LEFT JOIN LATERAL (
                SELECT version, backend_type FROM api_version
                WHERE api_id = a.id AND status = 'published'
                ORDER BY created_at DESC LIMIT 1
            ) v ON true
            WHERE a.status = 'published'{search_clause}
            ORDER BY a.updated_at DESC
            LIMIT ${idx} OFFSET ${idx + 1}
        """
        params.append(limit)
        params.append(offset)
        rows = await conn.fetch(list_sql, *params)

    items: list[PortalApiItem] = []
    all_categories: set[str] = set()
    all_tags: set[str] = set()
    for r in rows:
        tags_list: list[str] = r["tags"] if isinstance(r["tags"], (list, tuple)) else []
        items.append(PortalApiItem(
            api_id=str(r["id"]),
            name=r["name"],
            description=r["description"],
            category=r["category"] or "",
            tags=tags_list,
            base_path=str(r["base_path"]),
            visibility=str(r["visibility"]),
            backend_type=str(r["backend_type"]) if r["backend_type"] else "http",
            version=str(r["version"]) if r["version"] else "",
            updated_at=r["updated_at"].isoformat() if r["updated_at"] else "",
        ))
        if r["category"]:
            all_categories.add(str(r["category"]))
        for t in tags_list:
            all_tags.add(str(t))

    return PortalApiListResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        categories=sorted(all_categories),
        tags=sorted(all_tags),
    )


async def get_api_detail(api_id: str) -> PortalApiDetail:
    """取 API 详情（含全部版本列表）。"""
    async with db.db_session() as conn:
        api_row = await conn.fetchrow(
            "SELECT id, name, description, category, tags, base_path, visibility, status "
            "FROM api WHERE id = $1 AND status = 'published'",
            api_id,
        )
        if not api_row:
            raise ApiError(ErrorCode.NOT_FOUND, f"API {api_id} not found")

        ver_rows = await conn.fetch(
            """
            SELECT id, version, method, path, backend_type, status,
                   request_schema, response_schema, masking, ai_model, ai_streaming
            FROM api_version
            WHERE api_id = $1
            ORDER BY created_at DESC
            """,
            api_id,
        )

    tags_list: list[str] = api_row["tags"] if isinstance(api_row["tags"], (list, tuple)) else []
    versions: list[PortalVersionItem] = []
    for vr in ver_rows:
        versions.append(PortalVersionItem(
            version_id=str(vr["id"]),
            version=str(vr["version"]),
            method=str(vr["method"]),
            path=str(vr["path"]),
            backend_type=str(vr["backend_type"]),
            status=str(vr["status"]),
            request_schema=vr["request_schema"],
            response_schema=vr["response_schema"],
            masking=vr["masking"],
            ai_model=vr["ai_model"],
            ai_streaming=bool(vr["ai_streaming"]),
        ))

    return PortalApiDetail(
        api_id=str(api_row["id"]),
        name=api_row["name"],
        description=api_row["description"],
        category=api_row["category"] or "",
        tags=tags_list,
        base_path=str(api_row["base_path"]),
        visibility=str(api_row["visibility"]),
        api_status=str(api_row["status"]),
        versions=versions,
    )


async def try_api(payload: TryRequest) -> TryResponse:
    """在线调试：用 API Key 调通后端真实 URL，返回响应 + 延迟。
    
    backend_url 从 PG 直接读取（不经过 PortalVersionItem，避免暴露给前端）。
    """
    import time

    # 1. 查 API + version 元数据（含 backend_url）
    async with db.db_session() as conn:
        api_row = await conn.fetchrow(
            "SELECT id, base_path FROM api WHERE id = $1 AND status = 'published'",
            payload.api_id,
        )
        if not api_row:
            return TryResponse(status=404, error=f"API {payload.api_id} not found")

        if payload.version_id:
            ver_row = await conn.fetchrow(
                """SELECT backend_type, backend_url, method
                   FROM api_version WHERE id = $1""",
                payload.version_id,
            )
        else:
            ver_row = await conn.fetchrow(
                """SELECT backend_type, backend_url, method
                   FROM api_version WHERE api_id = $1 AND status = 'published'
                   ORDER BY created_at DESC LIMIT 1""",
                payload.api_id,
            )
        if not ver_row:
            return TryResponse(status=404, error="No published version found")

    # 2. 验证 API Key → 调 auth-svc
    settings = get_settings()
    async with httpx.AsyncClient(timeout=5.0) as c:
        r = await c.post(
            settings.auth_service_url,
            json={"api_key": payload.api_key},
        )
    if r.status_code != 200:
        return TryResponse(status=401, error="API Key 无效")

    # 3. 拼 backend_url，替换路径参数
    backend_url = ver_row["backend_url"]
    for k, v in payload.path_params.items():
        backend_url = backend_url.replace(f"{{{k}}}", v)

    # 4. 构造请求
    headers = {"X-API-Key": payload.api_key, "Content-Type": "application/json"}
    headers.update(payload.headers)

    # 5. 执行请求
    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=payload.timeout_ms / 1000) as c:
            resp = await c.request(
                method=payload.method,
                url=backend_url,
                headers=headers,
                params=payload.query_params,
                json=payload.body if payload.body is not None else None,
            )
    except httpx.TimeoutException:
        elapsed = int((time.perf_counter() - start) * 1000)
        return TryResponse(status=504, error="后端响应超时", latency_ms=elapsed)
    except httpx.RequestError as e:
        elapsed = int((time.perf_counter() - start) * 1000)
        return TryResponse(status=502, error=f"后端不可达: {e}", latency_ms=elapsed)

    elapsed = int((time.perf_counter() - start) * 1000)

    # 6. 解析响应体
    ct = resp.headers.get("content-type", "")
    try:
        resp_body: Any = resp.json() if "json" in ct else resp.text[:4096]
    except Exception:
        resp_body = resp.text[:4096]

    return TryResponse(
        status=resp.status_code,
        headers={"content-type": ct},
        body=resp_body,
        latency_ms=elapsed,
    )
```

- [ ] **Step 2: Verify import**

```bash
cd services/services/portal
python -c "from portal.repository import list_portal_apis, get_api_detail, try_api; print('OK')"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add services/services/portal/src/portal/repository.py
git commit -m "feat(portal-repository): API 目录 + 在线调试数据访问"
```

---

### Task 3: portal-bff routes — 注册 3 个新端点

**Files:**
- Modify: `services/services/portal/src/portal/routes.py`

**Interfaces:**
- Produces: 3 FastAPI routes consumed by client (frontend)

- [ ] **Step 1: Add imports to `routes.py`**

Add after the existing `from portal.models import ...` line:

```python
from portal.models import ApiKeyCreate, ApiKeyResponse, AppCreate, AppResponse, TryRequest
```

- [ ] **Step 2: Add new routes before the `# ========== app/key 自助` section**

Insert before the `# ========== app/key 自助（需 JWT → require_tenant）==========` comment:

```python
    # ========== API 目录（需 JWT）==========
    @app.get("/v1/portal/apis")
    async def list_portal_apis(
        search: str = "",
        category: str = "",
        tag: str = "",
        limit: int = 50,
        offset: int = 0,
    ):
        """API 目录列表 + 搜索/过滤/分页。"""
        ctx = require_tenant()
        return await repository.list_portal_apis(
            search=search, category=category, tag=tag,
            limit=min(limit, 200), offset=offset,
        )

    @app.get("/v1/portal/apis/{api_id}")
    async def get_api_detail(api_id: str):
        """API 详情（含版本列表 + schema）。"""
        require_tenant()
        return await repository.get_api_detail(api_id)

    @app.post("/v1/portal/try")
    async def try_endpoint(payload: TryRequest):
        """在线调试代理（用 API Key 调通后端）。"""
        require_tenant()
        return await repository.try_api(payload)
```

- [ ] **Step 3: Verify import**

```bash
cd services/services/portal
python -c "from portal.routes import register_routes; print('OK')"
```
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add services/services/portal/src/portal/routes.py
git commit -m "feat(portal-routes): API 目录 + 在线调试路由"
```

---

### Task 4: portal-bff 单测

**Files:**
- Modify: `services/services/portal/tests/test_routes.py`

**Interfaces:**
- Tests the 3 new endpoints via monkeypatch of repository functions
- Follows existing test patterns (mock repository, use client fixture from conftest.py)

- [ ] **Step 1: Add test for `GET /v1/portal/apis`**

```python
from portal.models import PortalApiListResponse, PortalApiItem


async def test_list_portal_apis(client, monkeypatch):
    """GET /v1/portal/apis 返回过滤/分页后的 API 列表。"""
    async def fake_list(**kw):
        return PortalApiListResponse(
            items=[
                PortalApiItem(
                    api_id="api_1", name="Test API", category="test",
                    tags=["foo"], base_path="/test", visibility="public",
                    backend_type="http", version="v1", updated_at="2026-07-13T00:00:00",
                )
            ],
            total=1, limit=50, offset=0,
            categories=["test"], tags=["foo"],
        )

    monkeypatch.setattr("portal.routes.repository.list_portal_apis", fake_list)

    r = await client.get("/v1/portal/apis?search=test&category=test&tag=foo")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["name"] == "Test API"
    assert body["categories"] == ["test"]
    assert body["tags"] == ["foo"]
```

- [ ] **Step 2: Add test for `GET /v1/portal/apis/{id}`**

```python
async def test_get_api_detail(client, monkeypatch):
    """GET /v1/portal/apis/{id} 返回 API 详情 + 版本列表。"""
    from portal.models import PortalApiDetail, PortalVersionItem

    async def fake_detail(api_id):
        return PortalApiDetail(
            api_id=api_id, name="Detail API", category="test",
            tags=[], base_path="/test", visibility="public",
            api_status="published",
            versions=[
                PortalVersionItem(
                    version_id="ver_1", version="v1", method="GET",
                    path="/echo", backend_type="http", status="published",
                    request_schema={"type": "object"},
                ),
            ],
        )

    monkeypatch.setattr("portal.routes.repository.get_api_detail", fake_detail)

    r = await client.get("/v1/portal/apis/api_1")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Detail API"
    assert len(body["versions"]) == 1
    assert body["versions"][0]["version"] == "v1"
```

- [ ] **Step 3: Add tests for `POST /v1/portal/try`**

```python
async def test_try_api_success(client, monkeypatch):
    """POST /v1/portal/try 成功返回后端响应 + 延迟。"""
    from portal.models import TryResponse

    async def fake_try(payload):
        return TryResponse(
            status=200,
            headers={"content-type": "application/json"},
            body={"ok": True},
            latency_ms=42,
        )

    monkeypatch.setattr("portal.routes.repository.try_api", fake_try)

    r = await client.post(
        "/v1/portal/try",
        json={
            "api_id": "api_1",
            "method": "GET",
            "api_key": "ak_test_valid",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == 200
    assert body["body"] == {"ok": True}
    assert body["latency_ms"] == 42
    assert body["error"] is None


async def test_try_api_key_invalid(client, monkeypatch):
    """POST /v1/portal/try 在 API Key 无效时返回 401 error。"""
    from portal.models import TryResponse

    async def fake_try(payload):
        return TryResponse(status=401, error="API Key 无效")

    monkeypatch.setattr("portal.routes.repository.try_api", fake_try)

    r = await client.post(
        "/v1/portal/try",
        json={
            "api_id": "api_1",
            "method": "GET",
            "api_key": "ak_bad",
        },
    )
    assert r.status_code == 200  # try 端点始终 200
    body = r.json()
    assert body["status"] == 401
    assert body["error"] is not None


async def test_try_api_backend_timeout(client, monkeypatch):
    """POST /v1/portal/try 在后端超时时返回 504 error + latency。"""
    from portal.models import TryResponse

    async def fake_try(payload):
        return TryResponse(status=504, error="后端响应超时", latency_ms=30000)

    monkeypatch.setattr("portal.routes.repository.try_api", fake_try)

    r = await client.post(
        "/v1/portal/try",
        json={
            "api_id": "api_1",
            "method": "GET",
            "api_key": "ak_test",
            "timeout_ms": 100,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == 504
    assert body["latency_ms"] == 30000


async def test_try_api_404(client, monkeypatch):
    """POST /v1/portal/try 在 API 不存在时返回 404 error。"""
    from portal.models import TryResponse

    async def fake_try(payload):
        return TryResponse(status=404, error="API not found")

    monkeypatch.setattr("portal.routes.repository.try_api", fake_try)

    r = await client.post(
        "/v1/portal/try",
        json={
            "api_id": "api_nonexistent",
            "method": "GET",
            "api_key": "ak_test",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == 404
```

- [ ] **Step 4: Run tests**

```bash
cd services/services/portal
python -m pytest tests/test_routes.py -v
```
Expected: all 4 existing tests + 5 new tests = 9 tests PASS

- [ ] **Step 5: Commit**

```bash
git add services/services/portal/tests/test_routes.py
git commit -m "test(portal): API 目录 + 在线调试路由单测"
```

---

### Task 5: Portal 前端 — API 目录页 (ApiCatalog.tsx)

**Files:**
- Create: `frontend/portal/src/pages/ApiCatalog.tsx`

**Interfaces:**
- Consumes: `api.get()` from `../api/client` — calls `GET /v1/portal/apis?search=&category=&tag=&limit=&offset=`
- Produces: A page component rendered at `/apis` route

- [ ] **Step 1: Create `ApiCatalog.tsx`**

```typescript
import { useEffect, useState, useRef } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { api } from '../api/client';

type BackendType = 'http' | 'ai_model' | 'async_task' | 'workflow';

interface ApiItem {
  api_id: string;
  name: string;
  description: string | null;
  category: string;
  tags: string[];
  base_path: string;
  visibility: string;
  backend_type: BackendType;
  version: string;
  updated_at: string;
}

interface ApiListResponse {
  items: ApiItem[];
  total: number;
  limit: number;
  offset: number;
  categories: string[];
  tags: string[];
}

const BACKEND_BADGE: Record<BackendType, { label: string; color: string }> = {
  http:       { label: 'HTTP',       color: 'bg-blue-100 text-blue-700' },
  ai_model:   { label: 'AI SSE',     color: 'bg-purple-100 text-purple-700' },
  async_task: { label: 'Async Task', color: 'bg-orange-100 text-orange-700' },
  workflow:   { label: 'Workflow',   color: 'bg-gray-100 text-gray-600' },
};

function timeAgo(iso: string): string {
  if (!iso) return '';
  const sec = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (sec < 60) return '刚刚';
  if (sec < 3600) return `${Math.floor(sec / 60)}分钟前`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}小时前`;
  return `${Math.floor(sec / 86400)}天前`;
}

export function ApiCatalog() {
  const nav = useNavigate();
  const [params, setParams] = useSearchParams();
  const [data, setData] = useState<ApiListResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [searchInput, setSearchInput] = useState(params.get('search') || '');
  const debounceRef = useRef<ReturnType<typeof setTimeout>>();

  const search = params.get('search') || '';
  const category = params.get('category') || '';
  const tag = params.get('tag') || '';
  const offset = parseInt(params.get('offset') || '0', 10);
  const limit = 50;

  const fetchApis = async () => {
    setLoading(true);
    setError('');
    try {
      const result = await api.get<ApiListResponse>('/v1/portal/apis', {
        search: search || undefined,
        category: category || undefined,
        tag: tag || undefined,
        limit,
        offset,
      });
      setData(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchApis();
  }, [search, category, tag, offset]);

  const updateParam = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    if (value) { next.set(key, value); } else { next.delete(key); }
    if (key !== 'offset') next.delete('offset');
    setParams(next);
  };

  const doSearch = () => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    updateParam('search', searchInput);
  };

  const onSearchChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    setSearchInput(e.target.value);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      updateParam('search', e.target.value);
    }, 300);
  };

  const pageCount = data ? Math.ceil(data.total / limit) : 0;
  const currentPage = Math.floor(offset / limit) + 1;

  return (
    <div className="max-w-4xl mx-auto p-4">
      <h1 className="text-2xl font-bold mb-4">API 目录</h1>

      {/* 搜索 + 过滤 */}
      <div className="flex gap-2 mb-4">
        <input
          className="flex-1 border rounded px-3 py-2"
          placeholder="搜索 API 名称或描述…（回车搜索）"
          value={searchInput}
          onChange={onSearchChange}
          onKeyDown={(e) => e.key === 'Enter' && doSearch()}
        />
        <select
          className="border rounded px-3 py-2"
          value={category}
          onChange={(e) => updateParam('category', e.target.value)}
        >
          <option value="">全部分类</option>
          {(data?.categories || []).map((c) => (
            <option key={c} value={c}>{c}</option>
          ))}
        </select>
        <select
          className="border rounded px-3 py-2"
          value={tag}
          onChange={(e) => updateParam('tag', e.target.value)}
        >
          <option value="">全部标签</option>
          {(data?.tags || []).map((t) => (
            <option key={t} value={t}>{t}</option>
          ))}
        </select>
      </div>

      {/* 加载态 */}
      {loading && (
        <div className="flex justify-center py-8">
          <div className="animate-spin w-8 h-8 border-4 border-blue-500 border-t-transparent rounded-full" />
        </div>
      )}

      {/* 错误态 */}
      {error && !loading && (
        <div className="bg-red-50 border border-red-200 rounded p-4 text-red-700">{error}</div>
      )}

      {/* 空态 */}
      {!loading && !error && data && data.items.length === 0 && (
        <div className="text-center py-12 text-gray-500">
          <p className="text-lg">没有找到匹配的 API</p>
          <p className="text-sm">试试其他关键词</p>
        </div>
      )}

      {/* API 卡片列表 */}
      {!loading && data && data.items.length > 0 && (
        <>
          <div className="space-y-3">
            {data.items.map((apiItem) => {
              const badge = BACKEND_BADGE[apiItem.backend_type] || BACKEND_BADGE.http;
              return (
                <div
                  key={apiItem.api_id}
                  className="border rounded-lg p-4 cursor-pointer hover:shadow-md transition-shadow"
                  onClick={() => nav(`/apis/${apiItem.api_id}`)}
                >
                  <div className="flex items-center justify-between mb-1">
                    <h3 className="text-lg font-semibold">{apiItem.name}</h3>
                    <span className={`text-xs font-medium px-2 py-0.5 rounded ${badge.color}`}>
                      {badge.label}
                    </span>
                  </div>
                  <p className="text-gray-600 text-sm mb-2">{apiItem.description || ''}</p>
                  <div className="flex items-center gap-2 text-xs text-gray-400">
                    {apiItem.tags.map((t) => (
                      <span key={t} className="bg-gray-100 px-1.5 py-0.5 rounded">#{t}</span>
                    ))}
                    <span className="ml-auto">{apiItem.version}</span>
                    <span>·</span>
                    <span>{timeAgo(apiItem.updated_at)}更新</span>
                  </div>
                </div>
              );
            })}
          </div>

          {/* 分页 */}
          {pageCount > 1 && (
            <div className="flex justify-center items-center gap-2 mt-6">
              <button
                className="px-3 py-1 border rounded disabled:opacity-50"
                disabled={offset === 0}
                onClick={() => updateParam('offset', String(offset - limit))}
              >
                上一页
              </button>
              {Array.from({ length: Math.min(pageCount, 10) }, (_, i) => (
                <button
                  key={i}
                  className={`px-3 py-1 border rounded ${currentPage === i + 1 ? 'bg-blue-500 text-white' : ''}`}
                  onClick={() => updateParam('offset', String(i * limit))}
                >
                  {i + 1}
                </button>
              ))}
              {pageCount > 10 && <span>…</span>}
              <button
                className="px-3 py-1 border rounded disabled:opacity-50"
                disabled={offset + limit >= data.total}
                onClick={() => updateParam('offset', String(offset + limit))}
              >
                下一页
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
```

- [ ] **Step 2: Verify TypeScript**

```bash
cd frontend/portal
npx tsc --noEmit 2>&1 | head -30
```
Expected: no errors

- [ ] **Step 3: Commit**

```bash
git add frontend/portal/src/pages/ApiCatalog.tsx
git commit -m "feat(portal-frontend): API 目录页（搜索/过滤/卡片/分页）"
```

---

### Task 6: Portal 前端 — API 详情 + 在线调试 (ApiDetail.tsx)

**Files:**
- Create: `frontend/portal/src/pages/ApiDetail.tsx`

- [ ] **Step 1: Create `ApiDetail.tsx`**

```typescript
import { useEffect, useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { api, ApiError } from '../api/client';

// ---- Types ----
interface VersionItem {
  version_id: string;
  version: string;
  method: string;
  path: string;
  backend_type: string;
  status: string;
  request_schema: Record<string, unknown> | null;
  response_schema: Record<string, unknown> | null;
  ai_streaming?: boolean;
}

interface ApiDetailData {
  api_id: string;
  name: string;
  description: string | null;
  category: string;
  tags: string[];
  base_path: string;
  visibility: string;
  api_status: string;
  versions: VersionItem[];
}

interface AppItem {
  id: string;
  name: string;
}

interface TryResponse {
  status: number;
  headers: Record<string, string>;
  body: unknown;
  latency_ms: number;
  error: string | null;
}

interface ExampleResponse {
  curl: string;
  python: string;
  javascript: string;
  notes: string[];
}

type Tab = 'docs' | 'schema' | 'examples' | 'try';

const BACKEND_BADGE: Record<string, { label: string; color: string }> = {
  http:       { label: 'HTTP',       color: 'bg-blue-100 text-blue-700' },
  ai_model:   { label: 'AI SSE',     color: 'bg-purple-100 text-purple-700' },
  async_task: { label: 'Async Task', color: 'bg-orange-100 text-orange-700' },
  workflow:   { label: 'Workflow',   color: 'bg-gray-100 text-gray-600' },
};

const TAB_LABEL: Record<Tab, string> = {
  docs: '文档说明',
  schema: '请求/响应',
  examples: '调用示例',
  try: '试试',
};

function StatusBadge({ status }: { status: number }) {
  const color =
    status < 300 ? 'bg-green-100 text-green-700' :
    status < 400 ? 'bg-yellow-100 text-yellow-700' :
    status < 500 ? 'bg-orange-100 text-orange-700' :
    'bg-red-100 text-red-700';
  return <span className={`font-mono text-sm px-2 py-0.5 rounded ${color}`}>{status}</span>;
}

function SchemaTable({ schema }: { schema: Record<string, unknown> | null }) {
  if (!schema || !schema.properties) {
    return <p className="text-gray-400 text-sm">无 schema 定义</p>;
  }
  const props = schema.properties as Record<string, unknown>;
  const required = (schema.required as string[]) || [];
  return (
    <table className="w-full text-sm border-collapse">
      <thead>
        <tr className="border-b bg-gray-50">
          <th className="text-left px-3 py-2">字段</th>
          <th className="text-left px-3 py-2">类型</th>
          <th className="text-left px-3 py-2">必填</th>
          <th className="text-left px-3 py-2">说明</th>
        </tr>
      </thead>
      <tbody>
        {Object.entries(props).map(([name, prop]) => {
          const p = prop as Record<string, unknown>;
          return (
            <tr key={name} className="border-b">
              <td className="px-3 py-1.5 font-mono">{name}</td>
              <td className="px-3 py-1.5">{String(p.type || 'any')}</td>
              <td className="px-3 py-1.5">{required.includes(name) ? '✓' : ''}</td>
              <td className="px-3 py-1.5 text-gray-500">{String(p.description || '')}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function CodeBlock({ code, lang }: { code: string; lang?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="relative">
      <pre className="bg-gray-900 text-gray-100 p-4 rounded text-sm overflow-x-auto">
        <code>{code}</code>
      </pre>
      <button
        className="absolute top-2 right-2 text-xs bg-gray-700 px-2 py-1 rounded text-gray-300 hover:bg-gray-600"
        onClick={() => { navigator.clipboard.writeText(code); setCopied(true); setTimeout(() => setCopied(false), 2000); }}
      >
        {copied ? '已复制' : '复制'}
      </button>
    </div>
  );
}

// ---- Component ----
export function ApiDetail() {
  const { id } = useParams<{ id: string }>();
  const nav = useNavigate();
  const [detail, setDetail] = useState<ApiDetailData | null>(null);
  const [examples, setExamples] = useState<ExampleResponse | null>(null);
  const [apps, setApps] = useState<AppItem[]>([]);
  const [activeTab, setActiveTab] = useState<Tab>('docs');
  const [selectedVerIdx, setSelectedVerIdx] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  // Try-it state
  const [selectedKey, setSelectedKey] = useState('');
  const [pathParams, setPathParams] = useState<Record<string, string>>({});
  const [queryParams, setQueryParams] = useState<{ key: string; value: string }[]>([]);
  const [bodyText, setBodyText] = useState('');
  const [tryResp, setTryResp] = useState<TryResponse | null>(null);
  const [tryLoading, setTryLoading] = useState(false);

  // Fetch API detail
  useEffect(() => {
    if (!id) return;
    setLoading(true);
    setError('');
    Promise.all([
      api.get<ApiDetailData>(`/v1/portal/apis/${id}`),
      api.get<AppItem[]>('/v1/portal/apps').catch(() => [] as AppItem[]),
    ])
      .then(([d, a]) => {
        setDetail(d);
        setApps(a);
        const v = d.versions[0];
        if (v) {
          if (v.request_schema) {
            const example = (v.request_schema as Record<string, unknown>).example;
            setBodyText(example ? JSON.stringify(example, null, 2) : '{\n  \n}');
          }
          const extracted: Record<string, string> = {};
          for (const m of (v.path || '').matchAll(/\{(\w+)\}/g)) {
            extracted[m[1]] = '';
          }
          setPathParams(extracted);
        }
      })
      .catch((err) => setError(err instanceof Error ? err.message : '加载失败'))
      .finally(() => setLoading(false));
  }, [id]);

  // Fetch examples when tab switches
  useEffect(() => {
    if (activeTab === 'examples' && id && !examples) {
      api.get<ExampleResponse>(`/v1/docs/apis/${id}/examples`)
        .then(setExamples)
        .catch(() => {});
    }
  }, [activeTab, id, examples]);

  const version = detail?.versions[selectedVerIdx] || null;

  // Try-it send
  const doTry = async () => {
    if (!id || !version || !selectedKey) return;
    setTryLoading(true);
    setTryResp(null);
    try {
      const resp = await api.post<TryResponse>('/v1/portal/try', {
        api_id: id,
        version_id: version.version_id,
        method: version.method,
        path_params: pathParams,
        query_params: Object.fromEntries(
          queryParams.filter((q) => q.key).map((q) => [q.key, q.value]),
        ),
        body: bodyText ? JSON.parse(bodyText) : null,
        api_key: selectedKey,
      });
      setTryResp(resp);
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : String(err);
      setTryResp({ status: 0, headers: {}, body: null, latency_ms: 0, error: msg });
    } finally {
      setTryLoading(false);
    }
  };

  // ---- Render ----
  if (loading) {
    return (
      <div className="flex justify-center py-12">
        <div className="animate-spin w-8 h-8 border-4 border-blue-500 border-t-transparent rounded-full" />
      </div>
    );
  }

  if (error || !detail) {
    return (
      <div className="max-w-4xl mx-auto p-4">
        <button onClick={() => nav('/apis')} className="text-blue-600 mb-4">&larr; 返回目录</button>
        <div className="bg-red-50 border border-red-200 rounded p-4 text-red-700">
          {error || 'API 不存在'}
        </div>
      </div>
    );
  }

  const badge = BACKEND_BADGE[version?.backend_type || ''] || BACKEND_BADGE.http;

  return (
    <div className="max-w-4xl mx-auto p-4">
      <button onClick={() => nav('/apis')} className="text-blue-600 mb-2">&larr; 返回目录</button>

      {/* Header */}
      <div className="flex items-center justify-between mb-2">
        <h1 className="text-2xl font-bold">{detail.name}</h1>
        <div className="flex items-center gap-2">
          <span className={`text-xs font-medium px-2 py-0.5 rounded ${badge.color}`}>{badge.label}</span>
          <span className="text-xs text-gray-400">{detail.visibility}</span>
        </div>
      </div>
      <p className="text-gray-500 text-sm mb-1">
        分类: {detail.category}
      </p>
      <p className="text-gray-600 mb-4">{detail.description || ''}</p>

      {/* Version selector */}
      {detail.versions.length > 1 && (
        <div className="mb-4 flex items-center gap-2">
          <label className="text-sm text-gray-500">版本:</label>
          <select
            className="border rounded px-3 py-1 text-sm"
            value={selectedVerIdx}
            onChange={(e) => {
              const idx = parseInt(e.target.value, 10);
              setSelectedVerIdx(idx);
              const v = detail.versions[idx];
              const ex: Record<string, string> = {};
              for (const m of (v?.path || '').matchAll(/\{(\w+)\}/g)) {
                ex[m[1]] = '';
              }
              setPathParams(ex);
              if (v?.request_schema) {
                const example = (v.request_schema as Record<string, unknown>).example;
                setBodyText(example ? JSON.stringify(example, null, 2) : '{\n  \n}');
              }
            }}
          >
            {detail.versions.map((v, i) => (
              <option key={v.version_id} value={i}>
                {v.version} ({v.status})
              </option>
            ))}
          </select>
          {version && (
            <span className="text-sm text-gray-500">
              {version.method} {version.path}
            </span>
          )}
        </div>
      )}

      {/* Tabs */}
      <div className="flex border-b mb-4">
        {(['docs', 'schema', 'examples', 'try'] as Tab[]).map((tab) => (
          <button
            key={tab}
            className={`px-4 py-2 text-sm font-medium border-b-2 -mb-px ${
              activeTab === tab
                ? 'border-blue-500 text-blue-600'
                : 'border-transparent text-gray-500 hover:text-gray-700'
            }`}
            onClick={() => setActiveTab(tab)}
          >
            {TAB_LABEL[tab]}
          </button>
        ))}
      </div>

      {/* Tab content */}
      {activeTab === 'docs' && (
        <div>
          <p>{detail.description || '暂无文档说明'}</p>
          <div className="mt-2">
            <span className="text-sm text-gray-500">标签: </span>
            {detail.tags.map((t) => (
              <span key={t} className="bg-gray-100 text-sm px-2 py-0.5 rounded mr-1">#{t}</span>
            ))}
          </div>
        </div>
      )}

      {activeTab === 'schema' && (
        <div className="space-y-6">
          <div>
            <h3 className="font-semibold text-sm mb-2">请求参数</h3>
            <SchemaTable schema={version?.request_schema || null} />
          </div>
          <div>
            <h3 className="font-semibold text-sm mb-2">响应参数</h3>
            <SchemaTable schema={version?.response_schema || null} />
          </div>
        </div>
      )}

      {activeTab === 'examples' && (
        <div className="space-y-4">
          {examples?.notes.map((n, i) => (
            <p key={i} className="text-yellow-700 bg-yellow-50 p-2 rounded text-sm">{n}</p>
          ))}
          {examples ? (
            <>
              <div>
                <h3 className="font-semibold text-sm mb-1">curl</h3>
                <CodeBlock code={examples.curl} lang="bash" />
              </div>
              <div>
                <h3 className="font-semibold text-sm mb-1">Python</h3>
                <CodeBlock code={examples.python} lang="python" />
              </div>
              <div>
                <h3 className="font-semibold text-sm mb-1">JavaScript</h3>
                <CodeBlock code={examples.javascript} lang="javascript" />
              </div>
            </>
          ) : (
            <p className="text-gray-400 text-sm">加载示例中…</p>
          )}
        </div>
      )}

      {activeTab === 'try' && version && (
        <div className="border rounded-lg p-4 space-y-4">
          {/* API Key selector */}
          <div>
            <label className="text-sm font-semibold">API Key</label>
            <select
              className="w-full border rounded px-3 py-2 mt-1"
              value={selectedKey}
              onChange={(e) => setSelectedKey(e.target.value)}
            >
              <option value="">-- 请选择 Key --</option>
              {apps.map((app) => (
                <option key={app.id} value={app.id}>{app.name}</option>
              ))}
            </select>
            {apps.length === 0 && (
              <p className="text-xs text-orange-600 mt-1">
                请先在「应用管理」中创建应用和 API Key
              </p>
            )}
          </div>

          {/* Path params */}
          {Object.keys(pathParams).length > 0 && (
            <div>
              <label className="text-sm font-semibold">路径参数</label>
              {Object.entries(pathParams).map(([key, val]) => (
                <div key={key} className="flex items-center gap-2 mt-1">
                  <span className="text-sm font-mono text-gray-500 w-24">{key}</span>
                  <input
                    className="flex-1 border rounded px-3 py-1.5 text-sm"
                    value={val}
                    onChange={(e) => setPathParams((prev) => ({ ...prev, [key]: e.target.value }))}
                  />
                </div>
              ))}
            </div>
          )}

          {/* Query params */}
          <div>
            <label className="text-sm font-semibold">查询参数</label>
            {queryParams.map((q, i) => (
              <div key={i} className="flex items-center gap-2 mt-1">
                <input
                  className="border rounded px-2 py-1 text-sm w-32"
                  placeholder="key"
                  value={q.key}
                  onChange={(e) => {
                    const next = [...queryParams];
                    next[i] = { ...next[i], key: e.target.value };
                    setQueryParams(next);
                  }}
                />
                <input
                  className="border rounded px-2 py-1 text-sm flex-1"
                  placeholder="value"
                  value={q.value}
                  onChange={(e) => {
                    const next = [...queryParams];
                    next[i] = { ...next[i], value: e.target.value };
                    setQueryParams(next);
                  }}
                />
                <button
                  className="text-red-500 text-sm"
                  onClick={() => setQueryParams(queryParams.filter((_, j) => j !== i))}
                >
                  删除
                </button>
              </div>
            ))}
            <button
              className="text-blue-600 text-sm mt-1"
              onClick={() => setQueryParams([...queryParams, { key: '', value: '' }])}
            >
              + Add
            </button>
          </div>

          {/* Request body */}
          <div>
            <label className="text-sm font-semibold">请求体 (JSON)</label>
            <textarea
              className="w-full border rounded px-3 py-2 text-sm font-mono mt-1"
              rows={8}
              value={bodyText}
              onChange={(e) => setBodyText(e.target.value)}
              placeholder='{"key": "value"}'
            />
          </div>

          {/* Send */}
          <button
            className="bg-blue-600 text-white px-6 py-2 rounded font-medium disabled:opacity-50"
            disabled={tryLoading || !selectedKey}
            onClick={doTry}
          >
            {tryLoading ? '发送中…' : '▶ Send'}
          </button>

          <button
            className="ml-2 px-4 py-2 border rounded"
            onClick={() => {
              setTryResp(null);
              setBodyText('{\n  \n}');
              setQueryParams([]);
            }}
          >
            Clear
          </button>

          {/* Response */}
          {tryResp && (
            <div className="border rounded p-4 bg-gray-50">
              <div className="flex items-center gap-2 mb-2">
                <StatusBadge status={tryResp.status} />
                {tryResp.error && <span className="text-red-600 text-sm">{tryResp.error}</span>}
                {!tryResp.error && tryResp.latency_ms > 0 && (
                  <span className="text-gray-400 text-sm">{tryResp.latency_ms}ms</span>
                )}
              </div>
              {tryResp.body !== null && tryResp.body !== undefined && (
                <pre className="bg-gray-900 text-gray-100 p-4 rounded text-sm overflow-x-auto">
                  <code>{JSON.stringify(tryResp.body, null, 2)}</code>
                </pre>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 2: Verify TypeScript**

```bash
cd frontend/portal
npx tsc --noEmit 2>&1 | head -30
```
Expected: no errors

- [ ] **Step 3: Commit**

```bash
git add frontend/portal/src/pages/ApiDetail.tsx
git commit -m "feat(portal-frontend): API 详情页（4 tab + try-it 控制台）"
```

---

### Task 7: 路由注册 + smoke 扩展 + lint

**Files:**
- Modify: `frontend/portal/src/App.tsx`
- Modify: `scripts/smoke/portal-onboarding.py`

- [ ] **Step 1: Update `App.tsx` to register new routes**

```typescript
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { Register } from './pages/Register';
import { Login } from './pages/Login';
import { Apps } from './pages/Apps';
import { ApiCatalog } from './pages/ApiCatalog';
import { ApiDetail } from './pages/ApiDetail';
import { useStore } from './store';

export default function App() {
  const auth = useStore((s) => s.auth);
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/register" element={<Register />} />
        <Route path="/login" element={<Login />} />
        <Route path="/apps" element={auth ? <Apps /> : <Navigate to="/login" />} />
        <Route path="/apis" element={auth ? <ApiCatalog /> : <Navigate to="/login" />} />
        <Route path="/apis/:id" element={auth ? <ApiDetail /> : <Navigate to="/login" />} />
        <Route path="*" element={<Navigate to={auth ? '/apis' : '/login'} />} />
      </Routes>
    </BrowserRouter>
  );
}
```

Note: default redirect changed from `/apps` to `/apis`（API 目录作为 Portal 首页）。

- [ ] **Step 2: Extend `scripts/smoke/portal-onboarding.py`**

Append before the final exit:

```python
# ===== ⑦ API 目录搜索 =====
print("\n=== ⑦ API 目录搜索 ===")
st, body = http("GET", f"{PORTAL_URL}/v1/portal/apis?search=smoke&limit=5")
data = json.loads(body)
assert st == 200, f"目录搜索失败: {st} {body}"
assert data["total"] >= 1, f"应至少找到 1 个 API，找到 {data['total']}"
smoke_api = [a for a in data["items"] if "smoke" in a["name"].lower()]
assert len(smoke_api) >= 1, f"应找到 smoke-sync API: {data['items']}"
api_id = smoke_api[0]["api_id"]
print(f"  ✅ 找到 smoke-sync API: {api_id}")

# ===== ⑧ 在线调试 =====
print("\n=== ⑧ 在线调试 try ===")
st, body = http(
    "POST",
    f"{PORTAL_URL}/v1/portal/try",
    headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    },
    data=json.dumps({
        "api_id": api_id,
        "method": "POST",
        "body": {"message": "hello"},
        "api_key": api_key,
    }).encode(),
)
try_data = json.loads(body)
assert st == 200, f"try 端点返回非 200: {st} {body}"
assert try_data.get("status") == 200, f"后端返回非 200: {try_data}"
assert try_data.get("latency_ms", -1) >= 0, f"缺少 latency_ms: {try_data}"
assert try_data.get("error") is None, f"有 error: {try_data}"
print(f"  ✅ try 成功: {try_data['latency_ms']}ms")
```

Also ensure `PORTAL_URL` is defined at the top (should be `http://127.0.0.1:8011`).

- [ ] **Step 3: Run lint**

```bash
ruff check services/services/portal/
mypy services/services/portal/
cd frontend/portal && npx tsc --noEmit
```
Expected: clean

- [ ] **Step 4: Run portal-bff tests**

```bash
python -m pytest services/services/portal/tests/test_routes.py -v
```
Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/portal/src/App.tsx scripts/smoke/portal-onboarding.py
git commit -m "feat(portal): 路由注册 + smoke 扩展"
```
