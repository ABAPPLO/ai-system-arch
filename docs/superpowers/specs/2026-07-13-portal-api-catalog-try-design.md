# Phase 3 第二切片「API 目录 + 在线调试」设计

> 日期：2026-07-13
> 阶段：Phase 3 开放（`docs/10-roadmap.md` §5）第 2 个子项目
> 关联 Phase 3 第一切片：`2026-07-12-phase3-portal-identity-foundation-design.md`（身份地基）
> 关联文档：`docs/07-developer-portal.md`、`docs/03-services.md`

## 1. Goal

让外部开发者能在 Portal 中端到端完成：浏览 API 目录 → 搜索/过滤 → 查看 API 详情（schema / 示例）→ **在线调试**（用自己已有的 API Key 调通一个真实 API 并看到响应）。这是 Portal 从「身份管理」到「API 消费」的关键一步。

### 1.1 现有状态

Phase 3 第一切片完成后：
- Portal 有注册/登录/应用管理 3 页
- 外部开发者能注册 → 建应用 → 拿 API Key
- 但 Portal 里看不到任何 API 信息，开发者必须靠外部文档了解有什么 API

### 1.2 本切片做

- **portal-bff 扩展**：`/v1/portal/apis`（列表+搜索）、`/v1/portal/apis/{id}`（详情）、`/v1/portal/try`（在线调试代理）
- **Portal 前端新增 2 页**：API 目录页（`/apis`）、API 详情页（`/apis/{id}`，含 try-it 控制台）
- **端到端 smoke 脚本扩展**：`scripts/smoke/portal-onboarding.py` 追加搜索 + try 两步

### 1.3 非目标（明确 defer）

- SDK 自动生成（路线图 Phase 3 后续切片，已 deferred）
- API 市场化/计费（Phase 4）
- Webhook 通知（后续切片）
- 高级搜索（全文检索、语义搜索——Phase 1 ILIKE 够用）
- 在线调试的历史记录（后续改进）

## 2. 架构总览

```
┌─────────────────────────────────────────────────────────────────┐
│                        Portal 前端 (5174)                        │
│                                                                 │
│  /apis           →  API 目录 + 搜索                              │
│  /apis/{id}      →  API 详情（文档 / schema / 示例 / try-it）    │
└──────────────────────────┬──────────────────────────────────────┘
                           │ JWT + JSON
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                     portal-bff (8011)                            │
│                                                                  │
│  GET  /v1/portal/apis         ←  API 列表 + 搜索                  │
│  GET  /v1/portal/apis/{id}    ←  API 详情 + 版本列表              │
│  POST /v1/portal/try          ←  在线调试（代理调后端）             │
└──────────────────────┬──────────────────────────────────────────┘
                       │ db_session (RLS)     │ httpx
                       ▼                      ▼
┌──────────────────────────┐    ┌──────────────────────────┐
│  PostgreSQL              │    │  后端业务服务              │
│  (api + api_version 表)  │    │  (真实 backend_url)       │
└──────────────────────────┘    └──────────────────────────┘
```

## 3. portal-bff 端点

### 3.1 `GET /v1/portal/apis` — API 列表 + 搜索

```python
@app.get("/v1/portal/apis")
async def list_portal_apis(
    search: str = "",
    category: str = "",
    tag: str = "",
    limit: int = 50,
    offset: int = 0,
):
```

**响应模型：**

```python
class PortalApiItem(BaseModel):
    api_id: str
    name: str
    description: str | None
    category: str
    tags: list[str]
    base_path: str
    visibility: str
    backend_type: str           # http / async_task / ai_model / workflow
    version: str                # 最新 published 版本
    updated_at: str

class PortalApiListResponse(BaseModel):
    items: list[PortalApiItem]
    total: int
    limit: int
    offset: int
    categories: list[str]       # 当前结果中的类别列表（前端过滤下拉用）
    tags: list[str]             # 当前结果中的标签列表
```

**数据来源**：PG `api` + `api_version` 表，通过 `db_session()`（RLS 自动按租户过滤）。

**SQL：**

```sql
SELECT a.id, a.name, a.description, a.category, a.tags,
       a.base_path, a.visibility, v.backend_type, v.version, a.updated_at
FROM api a
LEFT JOIN LATERAL (
    SELECT version, backend_type FROM api_version
    WHERE api_id = a.id AND status = 'published'
    ORDER BY created_at DESC LIMIT 1
) v ON true
WHERE a.status = 'published'
  AND ($1 = '' OR a.name ILIKE '%' || $1 || '%' OR a.description ILIKE '%' || $1 || '%')
  AND ($2 = '' OR a.category = $2)
  AND ($3 = '' OR $3 = ANY(a.tags))
ORDER BY a.updated_at DESC
LIMIT $4 OFFSET $5
```

**过滤说明：**
- `search`：同时对 name 和 description 做 ILIKE 模糊匹配
- `category`：精确匹配 category 字段
- `tag`：用 `ANY(a.tags)` 精确匹配单个标签
- 分页通过 `LIMIT/OFFSET` 实现
- RLS 确保 Portal 用户只能看到自己有权限的 API（visibility 对租户可见的）

**总数查询（用于分页）：**

```sql
SELECT COUNT(*) FROM api a
WHERE a.status = 'published'
  AND ($1 = '' OR a.name ILIKE '%' || $1 || '%' OR a.description ILIKE '%' || $1 || '%')
  AND ($2 = '' OR a.category = $2)
  AND ($3 = '' OR $3 = ANY(a.tags))
```

### 3.2 `GET /v1/portal/apis/{api_id}` — API 详情

```python
@app.get("/v1/portal/apis/{api_id}")
async def get_api_detail(api_id: str):
```

**响应模型：**

```python
class PortalVersionItem(BaseModel):
    version_id: str
    version: str
    method: str
    path: str
    backend_type: str
    status: str                     # published / deprecated / retired
    request_schema: dict | None
    response_schema: dict | None
    masking: dict | None            # 脱敏规则（Portal 提示用）
    ai_model: str | None
    ai_streaming: bool = False

class PortalApiDetail(BaseModel):
    api_id: str
    name: str
    description: str | None
    category: str
    tags: list[str]
    base_path: str
    visibility: str
    api_status: str
    versions: list[PortalVersionItem]
```

**实现**：从 PG 读 api 行 + 所有版本的 rows，组合返回。对 visibility=private 的 API，仅该租户成员可查（RLS 天然处理）。

### 3.3 `POST /v1/portal/try` — 在线调试

```python
@app.post("/v1/portal/try")
async def try_api(payload: TryRequest):
```

**请求模型：**

```python
class TryRequest(BaseModel):
    api_id: str
    version_id: str | None = None       # 不传则用最新 published
    method: str = "GET"
    path_params: dict[str, str] = {}
    query_params: dict[str, str] = {}
    headers: dict[str, str] = {}
    body: Any = None                    # JSON body
    api_key: str                        # 调用者的 API Key
    timeout_ms: int = 30000             # 前端可调超时
```

**响应模型：**

```python
class TryResponse(BaseModel):
    status: int
    headers: dict[str, str]             # 关键响应头
    body: Any                           # JSON body 或文本
    latency_ms: int                     # 后端响应时间
    error: str | None = None
```

**执行逻辑：**

```
1. 解析 api_id + version_id → 查 PG 取 api + version 元数据
   - 验证 api.status=published
   - RLS 确保当前租户可见

2. 验证 API Key → 调 auth-svc（同现有认证流）
   - 失败 → 返回 {error: "API Key 无效", status: 401}

3. 拼接完整 backend_url
   - 替换 {path_params} 占位符
   - 如 backend_url = "http://user-svc/v1/users/{user_id}"
     传入 {user_id: "usr_123"} → "http://user-svc/v1/users/usr_123"

4. 构造 httpx 请求
   - method + url + query_params + headers + json=body
   - timeout = timeout_ms / 1000

5. 执行请求，计算 latency_ms

6. 返回 TryResponse
   - 成功：透传 status/headers/body + latency_ms
   - httpx.RequestError → {error: "后端不可达", status: 502, latency_ms}
   - timeout → {error: "后端响应超时", status: 504, latency_ms}
```

**错误处理原则**：try 端点是调试工具，所有异常都被捕获并映射到 TryResponse.error，不抛 ApiError。前端始终收到 200 HTTP（TryResponse 内的 status 字段反映后端返回的实际 HTTP 状态）。

**安全**：`api_key` 只在服务端传递，不进前端日志/网络面板的 URL 参数。

## 4. Portal 前端

### 4.1 技术栈

同现有 Portal：React + TypeScript + Vite + Tailwind CSS 4 + Zustand。
新增 `frontend/portal/src/pages/ApiCatalog.tsx` 和 `frontend/portal/src/pages/ApiDetail.tsx`。

### 4.2 `/apis` — API 目录页

**布局：**

```
┌───────────────────────────────────────────────────────────┐
│  API 目录                                                   │
│                                                           │
│  🔍 [________________________]  类别:[全部 ▼]  标签:[全部 ▼]│
│                                                           │
│  ┌─────────────────────────────────────────────────────┐  │
│  │ 用户查询服务                         HTTP           │  │
│  │ 根据用户 ID 查询用户信息                             │  │
│  │ #user #query  v1 · 2小时前更新                       │  │
│  ├─────────────────────────────────────────────────────┤  │
│  │ LLM 对话接口                           AI SSE       │  │
│  │ GPT-4o 对话，流式响应                                │  │
│  │ #ai #llm #sse  v1 · 1天前更新                       │  │
│  ├─────────────────────────────────────────────────────┤  │
│  │ 批量导入用户                           Async Task   │  │
│  │ 最大 1w 条，异步回调                                │  │
│  │ #user #batch  v1 · 3天前更新                        │  │
│  └─────────────────────────────────────────────────────┘  │
│                                                           │
│  [上一页]  1  2  3 ...  [下一页]                           │
└───────────────────────────────────────────────────────────┘
```

**功能：**
- 搜索输入框：输入即搜（300ms debounce），调 `GET /v1/portal/apis?search=...`
- 类别下拉：从响应 `categories` 字段渲染，选中后调 `&category=...`
- 标签下拉：从响应 `tags` 字段渲染，选中后调 `&tag=...`
- API 卡片：显示名称、描述、标签（`#tag` Badge）、版本号、更新时间、后端类型（色标 Badge）
- 后端类型色标：
  - HTTP → 蓝色
  - AI SSE → 紫色（流式）
  - Async Task → 橙色
  - Workflow → 灰色
- 点击卡片 → 跳转 `/apis/{id}`
- 分页：后端分页，前端显示页码

**加载态：**
- 首次加载：居中 spinner
- 搜索/过滤中：卡片区域上半透明覆盖 + 小型 spinner（保持现有卡片可见）

**空态：**
- 无结果：插图 + "没有找到匹配的 API，试试其他关键词"
- 网络错误：toast "加载失败，请重试"

### 4.3 `/apis/{id}` — API 详情 + 在线调试

**布局：**

```
┌───────────────────────────────────────────────────────────┐
│  < 返回目录    用户查询服务                 可见性: 公开    │
│  类别: user-service · 2小时前更新                         │
│                                                           │
│  ┌── Tab 栏 ──────────────────────────────────────────┐   │
│  │  文档说明 │ 请求/响应 │ 调用示例 │ ▌试试 ▌         │   │
│  └────────────────────────────────────────────────────┘   │
│                                                           │
│  版本: [v1 ▼]  方法: GET  路径: /v1/users/{user_id}       │
│                                                           │
│  ┌── Try It ──────────────────────────────────────────┐   │
│  │  API Key: [▼ 选择已创建的 Key  ]                    │   │
│  │                                                     │   │
│  │  路径参数:                                          │   │
│  │    user_id: [usr_abc123                ]            │   │
│  │                                                     │   │
│  │  查询参数:          [+ Add]                         │   │
│  │    (无)                                             │   │
│  │                                                     │   │
│  │  请求体 (JSON):                                     │   │
│  │  ┌─────────────────────────────────────────┐        │   │
│  │  │ {                                       │        │   │
│  │  │   "user_id": "usr_abc123"               │        │   │
│  │  │ }                                       │        │   │
│  │  └─────────────────────────────────────────┘        │   │
│  │                                                     │   │
│  │  [▶ Send]  [Clear]                                 │   │
│  │                                                     │   │
│  │  ┌── Response (127ms) ──────────────────────┐       │   │
│  │  │  Status: 200 OK                          │       │   │
│  │  │                                           │       │   │
│  │  │  {                                       │       │   │
│  │  │    "user_id": "usr_abc123",             │       │   │
│  │  │    "name": "张三",                      │       │   │
│  │  │    "email": "z***@example.com"          │       │   │
│  │  │  }                                       │       │   │
│  │  └──────────────────────────────────────────┘       │   │
│  └─────────────────────────────────────────────────────┘   │
└───────────────────────────────────────────────────────────┘
```

**Tab 四个面板：**

| Tab | 内容 | 数据来源 |
|-----|------|---------|
| 文档说明 | API description + category + tags | `GET /v1/portal/apis/{id}` |
| 请求/响应 | JSON Schema 渲染为表格（字段/类型/必填/说明） | `version.request_schema` / `.response_schema` |
| 调用示例 | curl / Python / JS 代码 + 复制按钮 | 复用 docs-svc `/v1/docs/apis/{id}/examples` |
| 试试 | 在线调试控制台 |  |

**版本切换**：下拉选择版本 → 更新 method/path/schema → try-it 用新版本调。

**Try-it 控制台细节：**

| 字段 | 实现 |
|------|------|
| API Key 选择器 | `GET /v1/portal/apps` 拉应用列表 → 展平为 Key 下拉（key_id + key_prefix），用户选一个 |
| 路径参数 | 从 `version.path` 中提取 `{param}` 占位符 → 动态渲染输入框，label 为参数名 |
| 查询参数 | 默认空，用户通过 [+ Add] 添加 key-value 对 |
| 请求体 | textarea（monospace），预填 schema 示例值（从 request_schema example 或 _example_from_schema 生成） |
| Send 按钮 | 调 `POST /v1/portal/try`，请求期间 disabled + spinner |
| 响应区 | 状况码 Badge（色标：2xx 绿/3xx 黄/4xx 橙/5xx 红）+ 延迟 + 着色 JSON/文本 |

**Try-it 各状态：**

| 状态 | 响应区显示 |
|------|-----------|
| 请求中 | spinner + "发送请求中…" |
| 成功 (2xx/3xx) | 绿色 status badge + 响应体 + 延迟 |
| 客户端错误 (4xx) | 橙色 status badge + 响应体 + 延迟 |
| 服务端错误 (5xx) | 红色 status badge + 响应体 + 延迟 |
| 超时 | 红色 status 504 + "后端响应超时" + 实际耗时 |
| 网络不可达 | 红色 status 502 + "无法连接到后端服务" |
| API Key 无效 | 红色 status 401 + "API Key 无效，请在应用管理中检查" |
| Key 选择器为空 | 禁用 Send + 提示 "请先在「应用管理」中创建应用和 API Key" |

## 5. 现有文件变更

### 5.1 portal-bff

- `src/portal/routes.py` — 新增 `/v1/portal/apis`、`/v1/portal/apis/{id}`、`/v1/portal/try` 路由
- `src/portal/models.py` — 新增 `PortalApiItem`、`PortalApiListResponse`、`PortalApiDetail`、`PortalVersionItem`、`TryRequest`、`TryResponse`
- `src/portal/repository.py` — 新增 `list_portal_apis()`、`get_api_detail()`、`get_api_version_by_id()`（复用 docs-svc 类似逻辑）
- `tests/test_routes.py` — 新增 test cases

### 5.2 Portal 前端

- `frontend/portal/src/pages/ApiCatalog.tsx` — 新增 API 目录页
- `frontend/portal/src/pages/ApiDetail.tsx` — 新增 API 详情 + try-it 页
- `frontend/portal/src/App.tsx` — 注册 `/apis` + `/apis/:id` 路由
- `frontend/portal/src/api/client.ts` — 无变更（复用现有 api client）

### 5.3 端到端 smoke

- `scripts/smoke/portal-onboarding.py` — 追加两步：
  - ⑦ `GET /v1/portal/apis?search=smoke` → 列表含 `smoke-sync`
  - ⑧ `POST /v1/portal/try` 调 `smoke-sync/echo` → 200

## 6. Done 标准

- ✅ `GET /v1/portal/apis` — 搜索/过滤/分页正常
- ✅ `GET /v1/portal/apis/{id}` — 返回版本列表 + schema
- ✅ `POST /v1/portal/try` — 用 API Key 调通后端，返回响应 + 延迟
- ✅ Portal 前端 `/apis` 页 — 搜索 + 卡片列表 + 分页
- ✅ Portal 前端 `/apis/{id}` 页 — 4 tab（文档/请求响应/示例/试试）
- ✅ Try-it 控制台 — 选 Key + 填参 + 发请求 + 看响应（含超时/错误等边界）
- ✅ `ruff check` + `mypy` clean
- ✅ 端到端 smoke `portal-onboarding.py` GREEN

## 7. 风险

| 风险 | 影响 | 对策 |
|------|------|------|
| try-it 暴露后端 URL 给前端 | 中 | portal-bff 做代理，前端不直接接触 backend_url |
| try-it 成为 SSRF 入口 | 高 | 只允许调已注册 API 的 backend_url（走 PG 查询），不支持任意 URL |
| try-it 超时阻塞 portal-bff worker | 低 | timeout_ms 默认 30s，httpx timeout 硬限制 |
| 外部开发者看到 private API？ | 低 | RLS + visibility 检查双重保障 |
| API 目录数据量大时搜索慢 | 中 | ILIKE 有索引兜底，第一阶段简化；后续加 pg_trgm |

## 8. 实现顺序

1. portal-bff 数据层：`repository.py` — `list_portal_apis()` + `get_api_detail()` + `try_api()`
2. portal-bff 路由层：`routes.py` — 注册 3 个新端点
3. portal-bff 单测：`tests/test_routes.py`
4. Portal 前端 API 目录页：`ApiCatalog.tsx`
5. Portal 前端 API 详情页 + try-it：`ApiDetail.tsx`
6. 端到端 smoke 扩展
7. `ruff check` + `mypy`
