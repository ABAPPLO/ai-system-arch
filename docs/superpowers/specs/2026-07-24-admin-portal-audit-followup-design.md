# Admin/Portal 审计收尾（A3 编辑/删除 + P5 + P4）— 设计

- **日期**：2026-07-24
- **分支**：`feat/admin-audit-archive-apis-paging`（已含 A3 分页 + A4 归档入口，commit `8826911`，未合）。本轮在其上叠加 A3 编辑/删除 + P5 + P4，合并为一个 squash-PR（PR 范围 = A3 全量 + A4 + P5 + P4）。
- **base**：`origin/main`
- **来源**：#84 PR body 列出的「未含（后续候选）」清单中的 A3（Apis 编辑/删除+分页）、P5（Apps 缺 revoke/rotate/前缀展示）、P4（JWT 存 localStorage 的 CSP 评估）。A3 的分页部分与 A4 已在 `8826911` 完成；本 spec 覆盖剩余部分。

## 背景与事实基线（已核对代码）

- **A3**：`api-registry/routes.py` 有 `GET/POST /v1/apis`、`GET /v1/apis/{id}`、版本 `publish/deprecate/retire` 生命周期，**无** `PUT/PATCH/DELETE /v1/apis/{id}`。`change_request.apply_change()` 只处理 `publish/deprecate/retire`；`update`/`create` 是 stub（不做任何事）。
- `db_session()`（`apihub_core/db.py:196-199`）在事务内 `SET LOCAL app.tenant_id` **且** `set_config('app.is_platform_admin', ctx.is_platform_admin, true)` → RLS 策略对 platform_admin 旁路租户过滤。故 admin（Bearer JWT `is_platform_admin=true`）经 `db_session()` 即可读改任意租户的 `api` 行，无需 `admin_db_session`。
- `api_version.api_id text NOT NULL REFERENCES api(id)`（`01-schema.sql:123`），无 `ON DELETE` 子句 → **FK RESTRICT**：`api` 有任意版本时硬删会被数据库拒绝。
- `api` 表列：`id, tenant_id, name, description, category, base_path, tags, status, visibility`（无 `archived`）。`status` 目前仅 `create_api` 置 `'draft'`，从不变迁（真正生命周期在 `api_version` 上）。
- **P5**：`auth/routes.py` 已有 `GET /v1/apps/{app_id}/api-keys`、`DELETE /v1/api-keys/{key_id}`、`POST /v1/api-keys/{key_id}/hmac-secret/rotate`（R2e）。`ApiKeyListItem.id` 即 `key_id`。但 `ApiKeyListItem`（`auth/models.py:65`）**不含** `signing`/`hmac_enrolled`——前端无法判定哪把 key 可 rotate。portal-bff `portal/routes.py` 转发了 `apps` list/create + `apps/{id}/api-keys` POST（create），**未**转发 list/revoke/rotate。
- **P4**：`frontend/admin/src/api/client.ts` 把 access+refresh JWT 存 `localStorage`（`apihub_admin_token`/`apihub_admin_refresh`）。`admin/main.py` 无 `StaticFiles`（admin-svc 是纯 BFF，不伺服 SPA）。`index.html` 无任何 meta 安全头。全仓 grep 无 CSP。

## 决策（已与用户确认）

- **A3 合约**：直连 `PATCH` + 护栏硬删（不走 change-request 治理；与现有 create/version-lifecycle 同为直连端点）。
- **P4 范围**：只加 CSP + 评估文档；不做 httpOnly cookie 迁移（defer）。
- **P5**：经 BFF 转发（守 §9-B 不绕 BFF），rotate 仅 signing key 可见。

## A3 — Apis 编辑/删除

### 后端 `services/services/api-registry/src/api_registry/routes.py`

新增两个端点（放在 `get_api` 之后、`create_version` 之前，保持「静态段在 {param} 前」的路由顺序）：

1. `PATCH /v1/apis/{api_id}`
   - 请求体 `ApiUpdate`（新模型，`models.py`）：`name: str | None`、`description: str | None`、`category: str | None`、`tags: list[str] | None`、`visibility: str | None`，`model_config = ConfigDict(extra='forbid')`。**不含 `base_path`**——调用方传 `base_path` 即 422（从源头禁改，防断 APISIX 路由）。
   - 实现：`require_tenant()` → 仅 `UPDATE api SET <非 None 字段>, updated_at=NOW() WHERE id=$1`（RLS 强制同租户；admin platform_admin 旁路）。未命中行 → 404 `API_NOT_FOUND`。
   - 审计：`kafka.emit("audit-events", {action:"api.update", resource_type:"api", resource_id, detail: <updated fields>})`。
   - 返回更新后的完整行（同 `get_api` 形态）。

2. `DELETE /v1/apis/{api_id}`
   - 护栏：`require_tenant()` → 单事务内先 `SELECT EXISTS(SELECT 1 FROM api_version WHERE api_id=$1 AND status IN ('published','deprecated','reviewing'))`；为真 → 409（消息：「存在 published/deprecated/reviewing 版本，请先 publish→deprecate→retire 全部版本再删除」）。
   - 通过则同事务 `DELETE FROM api_version WHERE api_id=$1` 再 `DELETE FROM api WHERE id=$1`（绕 FK RESTRICT；两删在同一 `db_session` 事务里，任一失败整体回滚）。
   - 审计：`kafka.emit("audit-events", {action:"api.delete", resource_type:"api", resource_id})`。
   - 返回 `{id, status:"deleted"}`。

   > **已接受的取舍**：retired 版本可能仍被 ClickHouse 历史 call log 以 `version_id` 引用；硬删后 CH 日志的 `version_id` 变悬挂（日志仍在，仅无法 join 版本元数据）。这是 admin 显式二次确认的硬删动作，可接受；draft 版本无调用日志，删除无副作用。

### 前端 `frontend/admin/src/pages/Apis.tsx`

- 列表「操作」列：在「详情」旁加 **编辑**（打开 `ModalForm`，预填 name/description/category/tags/visibility，`base_path` 以只读字段展示并附说明「不可改」）+ **删除**（`Popconfirm` danger，标题二次确认）。
- client.ts 现有 `api` 只有 `get/post/put/del`，**需加 `patch`**（`request<T>(path, { method:'PATCH', body })`）。用 `PATCH` 而非 `PUT`——部分更新语义更准。
- 删除 → `api.del(...)`；捕获 `ApiError`，`status===409` 时 `message.warning` 展示护栏原因。
- 详情 `ApiDrawer` 顶部加「编辑」按钮，复用同一编辑 `ModalForm`（以 drawer 的 `data` 预填）。

## P5 — Apps key revoke/rotate/前缀展示

### 后端 `services/services/auth`

- `auth/models.py` `ApiKeyListItem` 加 `signing: bool = False`（语义 = hmac_enrolled，gate rotate 按钮可见性）。
- `auth/repository.py` `list_api_keys_for_app` 的 `SELECT` 补 `signing` 列（表列名 `signing`，见 `create_api_key` 已写 `signing=payload.signing`）。

### portal-bff `services/services/portal/src/portal/routes.py`

补 3 个薄转发（与现有 `apps` 转发同款 `_forward` helper）：

- `GET /v1/portal/apps/{app_id}/api-keys` → auth `GET /v1/apps/{app_id}/api-keys`
- `DELETE /v1/portal/api-keys/{key_id}` → auth `DELETE /v1/api-keys/{key_id}`
- `POST /v1/portal/api-keys/{key_id}/hmac-secret/rotate` → auth `POST /v1/api-keys/{key_id}/hmac-secret/rotate`

> 受保护端点（走标准中间件，JWT 注入 TenantContext），与现有 `portal_auth_*` handler 同模式。

### 前端 `frontend/portal/src/pages/Apps.tsx`

- 每个 app 行加「Key 管理」入口（`expandable.expandedRowRender` 展开行，或 Drawer），渲染 key 列表（`GET /v1/portal/apps/{app_id}/api-keys`）：
  - 列：前缀（`display_prefix` + `…`）、name、status（Tag）、created、last_used。
  - 操作：**吊销**（`Popconfirm` → `DELETE`，成功后 reload key 列表）；**轮换**（仅 `signing===true` 行渲染 → `POST .../hmac-secret/rotate` → 弹「仅显示一次」Modal 展示新 secret，复用现有 `newKey` Modal 样式）。
- 非 signing key 不显示轮换按钮（其 secret 为空，rotate 无意义）。

## P4 — CSP + 评估文档

### CSP 落地（admin + portal）

`frontend/admin/index.html` 与 `frontend/portal/index.html` 的 `<head>` 加：

```html
<meta http-equiv="Content-Security-Policy"
      content="default-src 'self';
               script-src 'self';
               style-src 'self' 'unsafe-inline';
               img-src 'self' data:;
               font-src 'self' data:;
               connect-src 'self';
               object-src 'none';
               base-uri 'self';
               form-action 'self'" />
```

- `script-src 'self'`（禁 inline/eval）：需验证 Vite prod build 无 inline `<script>`（React/AntD 不需要）。`index.html` 入口是 `<script type="module" src="/src/main.tsx">`（外部）。**实现时先 build 再核**——若 build 注入 modulepreload polyfill 等 inline script，改用 per-file hash；不要盲加 `'unsafe-inline'`（那会让 CSP 对 XSS 形同虚设）。
- `style-src 'unsafe-inline'`：AntD/runtime 注入内联样式，不可避免（接受）。
- `connect-src 'self'`：admin/portal 经同源 `/api/*` 代理调 BFF，同源成立。prod 若 SPA 与 API 不同源，需把 API origin 加入（实现时按 prod 拓扑确认；默认同源）。
- 选 `<meta>` 而非服务层 header：admin-svc 不伺服 SPA（无 StaticFiles），meta 与具体伺服层无关，dev(Vite)/prod 都生效。

### 评估文档 `docs/security-csp-eval.md`

- 残余风险：self-origin stored XSS 仍可读 `localStorage` 里的 JWT——CSP 仅降损（阻断外发 beacon via `connect-src`、阻断 inline/eval 注入），**不消除**。
- httpOnly cookie 迁移路径（defer）：BFF `set-cookie`（`Secure;HttpOnly;SameSite=Lax`）+ CSRF token + 前端改凭证读取 + CORS/credentials 调整；属新架构，单列后续轮。
- follow-up（meta 无法设）：`frame-ancestors 'none'` / `X-Frame-Options: DENY` 应在 SPA 伺服层（nginx/APISIX）以 header 设。

## 验证

- 后端单测：
  - `api-registry/tests`：`test_api_update_delete`——PATCH 改字段成功 / 传 `base_path` 422 / PATCH 不存在 404 / DELETE 有 published 版本 409 / DELETE 全 draft+retired 级联成功（版本与 api 同删）/ DELETE 不存在 404。RLS：跨租户 PATCH/DELETE 不可见（沿用现有 RLS 测试模式）。
  - `auth/tests`：`list_api_keys_for_app` 返回 `signing` 字段。
  - `portal/tests`：3 个新转发路由各 1 测试（沿用 `portal_auth_*` 测试模式）。
- 前端：`frontend/admin` + `frontend/portal` `typecheck` + `build` 双绿。
- `make lint`（ruff 0.6.x + mypy）绿（注意 ruff 用 CI 钉版 0.6.x，本地 0.15.21 不一致——按 r3f 教训用 `/tmp/ruff06`）。
- 运行时 e2e（dev-up 起 stack 真发 edit/delete/revoke/rotate）非阻断，按需。

## 范围外 / defer

- httpOnly cookie 迁移（P4 文档列路径）。
- `frame-ancestors` / `X-Frame-Options` 服务层 header（P4 follow-up）。
- A3 `base_path` 编辑（永远不可变）。
- A5 SSO prod apply（ops 侧，无 prod 访问）。

## 文件清单

**后端**
- `services/services/api-registry/src/api_registry/routes.py`（+ PATCH/DELETE）
- `services/services/api-registry/src/api_registry/models.py`（+ `ApiUpdate`）
- `services/services/api-registry/tests/test_*.py`（+ update/delete 测试）
- `services/services/auth/src/auth/models.py`（`ApiKeyListItem` + `signing`）
- `services/services/auth/src/auth/repository.py`（`list_api_keys_for_app` SELECT + signing）
- `services/services/portal/src/portal/routes.py`（+ 3 转发）
- `services/services/portal/tests/test_*.py`（+ 转发测试）

**前端**
- `frontend/admin/src/pages/Apis.tsx`（编辑/删除 UI）
- `frontend/admin/src/api/client.ts`（+ `patch` 方法）
- `frontend/portal/src/pages/Apps.tsx`（key 列表 + revoke/rotate + 前缀）
- `frontend/admin/index.html` + `frontend/portal/index.html`（CSP meta）

**文档**
- `docs/security-csp-eval.md`（新建）
