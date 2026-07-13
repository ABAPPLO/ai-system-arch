# Phase 3 第一切片「外部开发者身份地基」设计

> 日期：2026-07-12
> 阶段：Phase 3 开放（`docs/10-roadmap.md` §5）第 1 个子项目
> 关联 ADR：ADR-009（多租户）、ADR-011（实名：邮箱+手机）、ADR-012（Phase 3 内开放）
> 关联文档：`docs/07-developer-portal.md`、`docs/11-multi-tenant.md`、`docs/03-services.md`

## 1. Goal

让一个外部开发者能端到端完成：注册 → 邮箱验证 → 登录 → 在 `external-public` 租户建应用 → 拿 API Key → 用该 Key 经 APISIX 调通一个 `visibility=public` 的 API（收到 200）。这是 Phase 3 所有 Portal 功能的身份依附点 —— 没有外部开发者身份 + 应用/Key，目录/调试/用量都是「匿名浏览」而非真「对外开放」。

## 2. 范围（本切片做）

- **auth 服务扩展**：`/v1/auth/{register,verify-email,login}` + JWT 签发/verify 中间件
- **portal-bff 新服务**（镜像 admin）：薄代理身份端点 + 应用/Key 自助聚合
- **Portal 前端**（镜像 frontend/admin）：注册/登录/应用管理 3 页
- **dispatcher 扩展**：转发前 `visibility` 检查（public/tenant/private）
- 端到端 smoke 脚本 `scripts/smoke/portal-onboarding.py`

## 3. 非目标（明确 defer）

- 短信验证真接（dev stub）、企业实名 `enterprise`（营业执照+OCR）、SDK 生成（sdk-gen）、沙箱、计费、Webhook、API 目录搜索、在线调试 `/try`、文档 i18n → 后续切片
- 真接 SMTP/短信网关（dev 用 stub，prod 接入非本切片）
- 显式 `app_api_grant` 表（visibility 三级够用；细粒度授权后续演进）
- HTTP-only cookie session（已选 JWT）
- JWT refresh token（第一切片仅 access token；refresh 后续加）

## 4. 核心决策（brainstorming 锁定）

1. **切片边界**：端到端闭环（注册→…→APISIX 调通 200），非仅「身份+应用」。理由：能用 Key 调通 API 才是身份地基成立的证明。
2. **人认证**：**JWT**（user_id + tenant_id）；机器调用仍用应用 API Key 经 APISIX。人/机凭证分离，概念最清晰。
3. **身份逻辑归属**：**auth 服务扩展**。portal-bff 保持薄代理，不 own 身份逻辑；auth 成为统一身份服务。
4. **授权链路**：**visibility 驱动**，dispatcher 转发前检查 `api.visibility`（public/tenant/private）。消费已有但闲置的字段，不加表（YAGNI）。

## 5. 组件设计

### 5.1 auth 服务扩展（`services/services/auth`）

现有 auth 只做 `/v1/apikey/verify`。新增面向 Portal 的身份端点：

- `POST /v1/auth/register` `{email, password, phone}` → 写 `user_account`(status=pending, verification_level=email, password_hash=bcrypt) + 生成邮箱 token 存 Redis(`t:verify:{token}` → user_id, TTL 24h)。**dev stub**：token 写日志 + 响应返回（不真发邮件）。
- `GET /v1/auth/verify-email?token=xxx` → 校验 token → 标 email verified + 加 `tenant_member`(external-public) + status=active。
- `POST /v1/auth/login` `{email, password}` → bcrypt 校验 + 检查 `status='active'`（未验证 = pending → 403）→ 签 JWT(user_id, tenant_id=external-public, is_platform_admin=false)，返回 access_token。
- **JWT verify 中间件**：portal-bff 带 JWT 调下游，auth 提供 verify（复用/扩展 `apihub_core.auth.authenticate_request`，加 JWT 路径，与现有 API-Key 路径并存）。

JWT 用现有 `JWT_SECRET`（settings 已有）；access token TTL 2h。

### 5.2 portal-bff（`services/services/portal`，镜像 admin）

`main.py` 调 `create_app("portal", build_routes=..., skip_auth_paths={/v1/portal/auth/register, /v1/portal/auth/verify-email, /v1/portal/auth/login, /health, /docs})`。

`routes.py`：
- `/v1/portal/auth/*` → 转发 auth 的 register/verify-email/login
- `/v1/portal/apps` → 应用 CRUD（第一切片：portal-bff 内薄封装直写 `app` 表，tenant 从 JWT 取；后续可改转发 api-registry/admin 的 app 端点）
- `/v1/portal/apps/{id}/keys` → API Key 生成（写 `api_key` 表，明文返回一次）

JWT 透传：portal-bff 先 verify JWT 拿 TenantContext，再转发/处理。

### 5.3 Portal 前端（`frontend/portal`，镜像 frontend/admin）

技术栈同 frontend/admin：React + Vite + TS + Tailwind + Zustand。

3 页：注册（email+password+phone）、登录（email+password，JWT 存 localStorage）、应用管理（建 app + 生成/查看 Key，前 8 位明文 + 创建时全量显示一次）。

`api/client.ts` 镜像 admin 的，但带 `Authorization: Bearer <jwt>`（而非 admin 的 `X-API-Key`）。

### 5.4 dispatcher visibility 检查（`services/services/dispatcher`）

转发前：查目标 api 的 `visibility` + caller 的 tenant_id：
- `public` → 放行（任何有效 Key，含 external-public）
- `tenant` → caller.tenant_id == api.tenant_id
- `private` → caller.tenant_id == api.tenant_id AND caller.is_platform_admin

不满足 → 403。api.visibility 可走 Redis 热缓存避免每请求查库。

## 6. 数据模型（全复用，无新表）

- `user_account`（`01-schema.sql:32`）：已有 email/phone/password_hash/verification_level/status
- `tenant` external-public（`02-seed.sql` 已 seed，id=3）
- `app`（`01-schema.sql:62`）+ `api_key`（`:76`）
- `api.visibility`（`:109`，CHECK private/tenant/public）→ **首次消费**
- 邮箱 token：Redis `t:verify:{token}`（不改表）
- 验证状态：`user_account` **无 `email_verified` 列**（确认 schema:32-45），用 `status='pending'`(注册) → `'active'`(邮箱验证后) 表达；`status` 无 CHECK 约束可自由取值（default 'active' 仅影响 seed，新注册显式置 'pending'）。login 检查 `status='active'`

## 7. 端到端流程

```
①注册   Portal→portal-bff→auth/register : 写 user_account(pending) + token 存 Redis
                              dev stub: token 写日志+响应返回
②验证   Portal→portal-bff→auth/verify-email?token : email verified + 加 tenant_member(external-public) + active
③登录   Portal→portal-bff→auth/login : bcrypt 校验 → 签 JWT(user_id, tenant_id=external-public) → Portal 存 localStorage
④建应用 Portal(JWT)→portal-bff→/v1/portal/apps : 建 app(tenant=external-public)
⑤拿 Key  Portal(JWT)→portal-bff→/v1/portal/apps/{id}/keys : 生成 ak_xxx，明文返回一次
⑥调用   外部 Key→APISIX /dispatch/{public-api}→key-auth→dispatcher : 查 visibility=public→放行→转发→200
```

调通靶子：seed 的 `smoke-sync` API 标 `visibility=public`。

## 8. 错误处理

- 邮箱已存在 → 409（dev 简化；prod 安全考虑可改为统一发邮件不泄露是否存在）
- token 过期/无效 → 400
- 密码错 → 401；邮箱未验证 → 403
- visibility 不匹配 → 403；Key 无效/过期 → 401（APISIX key-auth）
- 统一 `ApiError`（apihub_core 已有）

## 9. 测试策略

- **auth**：register/verify/login 单测（stub Redis）；bcrypt；JWT 签发/verify；重复邮箱 409；未验证登录 403
- **portal-bff**：转发正确性（mock auth + app 端点）；JWT 透传；skip_auth_paths 生效
- **dispatcher**：visibility 三级单测（public 放行 / tenant 隔离 / private 拒绝）
- **RLS**：external-public 只见自己的 app/key（跨租户 403）
- **端到端**：`scripts/smoke/portal-onboarding.py`（镜像 `k8s-links.py`）：①注册 ②验证 ③登录拿 JWT ④建应用 ⑤拿 Key ⑥Key 经 APISIX 调 smoke-sync(visibility=public) = 200

## 10. Done 标准

- 外部开发者端到端 ①→⑥，最后用 Key 调通 public API 收到 200
- portal-bff + Portal 前端本地可跑（`make run-portal` + `make run-portal-frontend`）
- dispatcher visibility 单测绿
- 端到端 smoke GREEN
- `ruff check` + `mypy` clean

## 11. 风险 + 演进

- **JWT access 无 refresh**：第一切片可接受；后续加 refresh token + 黑名单
- **visibility 粗粒度**：后续需要撤回个别 app 授权 → 加 `app_api_grant` 表（docs/11 §6.4 已设计）
- **邮件 stub**：prod 上线前必须接 SMTP + 真验证邮件
- **APISIX key-auth consumer 承载量**：consumer 白名单模式能否承载大量外部 Key → 评估是否切 serverless key-auth 或 auth-svc 插件（第一切片用现有 consumer 模式，外部 Key 量小）
- **app 自助端点归属**：第一切片 portal-bff 内封装直写 app 表；后续若 api-registry/admin 櫸露对外 app 端点，改转发
