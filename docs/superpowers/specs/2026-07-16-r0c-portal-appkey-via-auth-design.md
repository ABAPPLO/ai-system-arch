# R0c spec — portal app/key 走 auth（服务边界第一步）

日期：2026-07-16 · 分支 `fix/r0c-portal-appkey-via-auth` · 依据：审计 `phase4-audit-findings.md` §9-B + fix-program `2026-07-15-apihub-fix-program-design.md` §5 Wave 0 R0c 行。

## 问题（架构根因）

设计 §3.5 定 auth 拥有 App/Key 聚合，但 **portal-bff 越权直写 `app`/`api_key` 表**（`portal/repository.py:25-92`），绕过 auth。审计 §9-B 判这是 §2-§4 一堆字段/序列化/ID 漂移集成 bug 的架构根因：没有"谁拥有哪个聚合"的硬规则，多服务共写共读同一批表。

portal 当初绕过的理由（`repository.py:1-4` 注释）写的是"auth `/v1/apps` 走 X-API-Key middleware，而 Portal 是 JWT 人认证"。**该理由已不成立**：`apihub_core/auth.py:35-47` 的 `authenticate_request` 对 `eyJ` 开头的 Bearer JWT 本地验签并注入 `TenantContext`，auth 自身中间件（`middleware.py:81`）对 `X-API-Key` 与 `Authorization: Bearer` **一视同仁**。portal 现在也已在 `/account`、`/consent`、`/export` 端点上把用户 JWT 原样转发给 auth（`portal/routes.py:68-134`）。

结论：R0c 不是新鉴权方案，是"补齐 auth 的 app 管理端点 + portal 改为薄 BFF 转发"，与现有 account/consent 转发同款。

## 走法选型（已定：A）

- **A. 薄 BFF 转发用户 JWT（采纳）** — portal 转发 `Authorization: Bearer <JWT>` 给 auth，auth 中间件本地验签 → `ctx.tenant_id` 建表（RLS 兜底）。与现有 portal BFF 模式一致、无需新凭证、租户来自 JWT 不可伪造。
- B. 服务间 token（portal 以服务身份调 auth、body 传 tenant_id）—— 需新凭证+密钥管理，auth 得信任调用方 tenant_id（伪造面），偏离现有模式，加重 §9-C 扇出税。弃。
- C. 共享 repository 库（SQL 抽进 apihub-core）—— 把"共写同表"合法化，正是病根本身，根本没修边界。弃。

每次 app/key 操作多一跳内部 HTTP 可接受：开发者自助是低频非热路径。

## 改动清单

### ① auth 补 app 管理端点（key 端点已有，复用）

现状：auth 只有 key 端点（`POST /v1/apps/{app_id}/api-keys`、`GET ...`、`DELETE /v1/api-keys/{id}`），**无 app 管理端点**。

- `auth/models.py`：新增 `AppCreate{name: str (2-64), type: str = "external"}`、`AppResponse{id, name, tenant_id, type, status}`（字段对齐 portal 现有契约）。
- `auth/repository.py`：新增
  - `create_app(*, tenant_id, name, app_type) -> dict`：`INSERT INTO app (id, tenant_id, name, type, status) VALUES ($1..,'active')`，走 `db_session`（RLS 同租户），id 用 `app_{uuid4().hex[:16]}`（与现有 `key_{uuid4().hex[:16]}` 风格一致；存量 app 不受影响，id 仅字符串 PK）。返回 `{id,name,tenant_id,type,status:'active'}`。
  - `list_apps_for_tenant(tenant_id) -> list[dict]`：`SELECT id,name,tenant_id,type,status FROM app WHERE tenant_id=$1 ORDER BY created_at DESC`，走 `db_session`。
- `auth/routes.py`：新增（均走标准中间件，JWT Bearer 即可，handler 内 `require_tenant()` 取 `ctx.tenant_id`）
  - `POST /v1/apps`（body `AppCreate`，status 201）→ `create_app`
  - `GET /v1/apps`（response `list[AppResponse]`）→ `list_apps_for_tenant(ctx.tenant_id)`
- 复用现有 `POST /v1/apps/{app_id}/api-keys`（`routes.py:129`）覆盖 portal 的 create_key。

### ② portal 删直写、改转发（与 account/consent 转发同款）

- `portal/routes.py`：`create_app` / `list_apps` / `create_api_key` 三端点改为用现有 `_forward(method, path, headers=..., json=...)` 调 auth，转发 `Authorization` 头：
  - `POST /v1/portal/apps` → `POST {auth_base}/v1/apps`
  - `GET /v1/portal/apps` → `GET {auth_base}/v1/apps`
  - `POST /v1/portal/apps/{app_id}/api-keys` → `POST {auth_base}/v1/apps/{app_id}/api-keys`
  - 非 2xx 按 `portal_delete_account`（`routes.py:68-83`）同款包成 `ApiError` 冒泡（404 = app 不属本租户/不存在）。
  - **字段映射**：auth `ApiKeyResponse` 返回 `display_prefix`，portal 契约是 `key_prefix` → 转发后 `{"key_prefix": body["display_prefix"], ...}` 仅取 `{id,app_id,name,key_prefix,api_key}`，丢弃 `scopes/expires_at/created_at`（portal 不暴露）。保持前端契约不破。
- `portal/repository.py`：删除 `create_app_for_user`、`list_apps_for_user`、`create_api_key_for_app` 三个直写函数，删顶部 `from auth.apikey import generate_api_key` 导入（不再需要）及过时注释。`list_portal_apis`/`get_api_detail`/`try_api`/计费相关函数**不动**（非 app/key 聚合）。

### ③ owner 文档

- 新建 `docs/aggregate-ownership.md`：一张表列资源 → 归属服务 + 写权限边界：
  | 资源 | 归属服务 | 其它服务 |
  |---|---|---|
  | `app` / `api_key` | auth | 只能调 auth API，禁止直写/直读 |
  | `audit_log` / `audit_events` | admin | 调 admin API |
  | `api` / `api_version` | api-registry | 调 api-registry API |
  | `subscription` / `billing_record` | billing | 调 billing API |
  | quota 计数（Redis） | quota | 调 quota API |
  | 调用日志（ClickHouse） | trace-svc 只读聚合 | — |
  + 一条硬规则：**BFF（portal/admin）是聚合/转发层，不得直写领域服务的表；跨聚合只能走拥有方 API**。注明这是 §9-B 的架构护栏，后续轮次（admin→audit 等）照此推进。
- `CLAUDE.md`「Architecture」节加一行引用 `docs/aggregate-ownership.md`。

## 验证（禁 smoke 脚本绕生产者，同前几轮方法论）

- **边界硬断言**：`grep -rnE "INSERT INTO (app|api_key)\b|FROM (app|api_key)\b" services/services/portal/` → 0 命中（直写直读全清）。
- **auth 单测**：`POST /v1/apps` 建出 `AppResponse` 且 `tenant_id==ctx.tenant_id`；`GET /v1/apps` 只返本租户 app（跨租户 RLS 过滤）。复用 `auth/tests/conftest.py` 的 JWT client fixture（注入固定 `TenantContext`），镜像现有 key 端点测试写法。
- **portal 单测**：三端点改为 stub `_forward`（或 monkeypatch `httpx.AsyncClient`）返回 auth 形状，断言 portal 不再触达 PG、字段映射正确（`key_prefix` 来自 `display_prefix`）；`conftest.py` 的 `client` fixture 不变。现有 `portal/tests/test_routes.py` 里 app/key 相关用例同步从"stub PG"改为"stub httpx→auth"——这是本轮主要测试改造量。
- **端到端（`make dev-up` 后）**：portal 带真实 JWT `POST /v1/portal/apps` → auth 落 `app` 表；`POST .../api-keys` → 拿到明文 key；该 key 调 dispatcher `/v1/apikey/verify` 通过（key 真实可用，链路打通）。
- **回归**：auth 全量 `test_*`、portal `test_routes.py` 全绿；`make lint` 绿。

## 不做（R0c 边界）

- admin 直写 `audit`、quota/billing 读 CH（别的聚合，后续轮次按 owner 文档推进）。
- portal 不新增 list-keys / revoke 端点（现有 portal 前端没有，非回归；YAGNI）。
- `try_api` 调 auth `/v1/apikey/verify`（本就正确、非边界泄漏）不动。
- app_id 格式不迁移存量数据（仅新 app 用 uuid 风格 id）。

## 风险

- portal 现有 `test_routes.py` app/key 用例多半 stub PG，改转发后要改成 stub httpx → auth —— 测试改造是本轮主要工作量，非生产代码风险。
- 转发后 auth 5xx 会冒泡，portal 必须按 `portal_delete_account` 同款包 ApiError（已有模式可抄，非新风险）。
- `auth_base` 由 `settings.auth_service_url.rsplit("/",3)[0]` 推导（`routes.py:21`），新端点路径 `/v1/apps` 与现有 `/v1/auth/*` 同 base，无双 `/v1/` 风险。

## 步骤（粗，细化交 writing-plans）

1. auth：加 `AppCreate`/`AppResponse` 模型 + `create_app`/`list_apps_for_tenant` repo + `POST/GET /v1/apps` 路由 + 单测（TDD 先写失败测试）。
2. portal：`routes.py` 三端点改 `_forward` 转发 + 字段映射；`repository.py` 删三直写函数 + 过时导入/注释。
3. 新建 `docs/aggregate-ownership.md` + `CLAUDE.md` 引用一行。
4. grep 边界断言 0 命中；auth + portal 测试全绿；`make lint` 绿。
5. （可选）`make dev-up` 端到端打通 key 真实可用。
6. commit → 一个 squash-PR（push/merge 仅在用户要求时）。
