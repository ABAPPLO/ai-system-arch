# Admin 控制台 SSO（钉钉 OAuth）设计

日期：2026-07-23 ｜ 状态：设计待评审，实现交独立会话

## 1. 背景与目标

当前 admin 控制台登录（`frontend/admin/src/pages/Login.tsx`）是 **dev stub**：表单填用户名 + `X-API-Key`，调 dashboard 验 key 有效性，但 **`user` 身份（`isPlatformAdmin`、`tenantId`）由前端硬编码伪造**。注释承认 Phase 3 才接 SSO。这是安全洞（任何能拿到一个有效 key 的人都能前端伪造成超管视角）。

目标：用 **钉钉 OAuth2** 替换 dev stub，admin 浏览器登录走真实 IdP，身份（user/tenant/超管）由后端在 JWT 中签发，前端不再伪造。仅 admin 控制台（portal 外部开发者保留现有邮箱/密码注册登录）。

## 2. 关键使能事实（已核实，无需新建鉴权通路）

- `apihub_core/auth.py::authenticate_request`（L80）已支持 **Bearer JWT**：`jwt_utils.is_jwt(api_key)` 为真时用 `settings.jwt_secret` 解码，从 payload 取 `tenant_id` / `is_platform_admin` 设入 `TenantContext`。中间件从 `X-API-Key` **或** `Authorization: Bearer` 提取 token（见 `middleware.py`）。
- auth-svc 已有 JWT 签发设施：`/v1/auth/login`（email/password）→ `identity.login` 签 access+refresh；`jwt_utils.decode_token`/`create_token` 可复用。
- 钉钉集成已存在于 notification（审批/工作通知），故 corp 凭据可复用同源配置。

→ 结论：admin SSO = 加「钉钉 OAuth 登录 → 签 JWT」链路 + 前端从 `X-API-Key` 切到 `Bearer JWT`，**不触动现有数据面鉴权**。

## 3. 登录流程

```
浏览器                admin 前端            auth-svc              钉钉 OAuth
   |  点"钉钉登录"        |                     |                       |
   |-------------------->| GET /v1/auth/dingtalk/authorize            |
   |                     |  <─ 重定向 URL（含 state）                  |
   |<── 跳转钉钉扫码页 ───────────────────────────────────────>|
   |   扫码同意           |                     |                       |
   |── code+state 回跳 /login/callback?code=.. ───────────────>|
   |                     | POST /v1/auth/dingtalk/callback {code,state}|
   |                     |                     |  换 userAccessToken     |
   |                     |                     |  取 unionId/nick ──────>|
   |                     |                     |  upsert user_account   |
   |                     |                     |  签 JWT(tenant,admin)  |
   |                     |  <── {access_jwt, refresh, user}            |
   |<── 存 JWT+user，进 /                    |                     |
```

## 4. 后端改动（auth-svc）

新增两个端点（`services/services/auth/src/auth/routes.py`，skip APIKey middleware）：

- `GET /v1/auth/dingtalk/authorize?redirect=<admin 回跳>`
  → 返回钉钉授权 URL：`https://login.dingtalk.com/oauth2/auth?client_id=..&redirect_uri=..&response_type=code&scope=openid&state=<随机>&prompt=consent`。`state` 存 Redis（`t:sso:state:{state}` → redirect，TTL 600s）防 CSRF。
- `POST /v1/auth/dingtalk/callback {code, state}`
  → ① 校验 state；② POST `https://api.dingtalk.com/v1.0/oauth2/userAccessToken`（client_id/secret/grant_type=authorization_code）换 token；③ GET `https://api.dingtalk.com/v1.0/contact/users/me` 取 `unionId`/`nick`；④ `identity.upsert_sso_user(union_id, name, provider='dingtalk')`：有则取 `user_account`，无则建（tenant=`platform`，加入 platform 租户成员）；⑤ 判 `is_platform_admin`（见 §6）；⑥ 签 access JWT（payload：`sub=user_id, tenant_id='platform', is_platform_admin, exp`）+ refresh；⑦ 返回 `{access_token, refresh_token, user:{id,name,is_platform_admin,tenant_id}}`。

依赖：auth-svc 加 `httpx`（已是依赖）。钉钉 client_id/secret 走 `Settings`（`dingtalk_client_id` / `dingtalk_client_secret` / `dingtalk_corp_id`）。

## 5. 前端改动（admin）

- `Login.tsx`：去 dev stub；「钉钉登录」按钮 → 调 `/api/auth/v1/auth/dingtalk/authorize`（或直接拼 URL）→ `window.location` 跳转。
- 新增 `LoginCallback.tsx`（路由 `/login/callback`）：从 query 取 `code/state` → POST callback → 存 JWT+user → `navigate('/')`。
- `api/client.ts`：鉴权从 `X-API-Key` 切到 `Authorization: Bearer <jwt>`；`getAuth()` 读 JWT+user；401 → refresh 一次（复用 portal client 的 refresh 逻辑）或回登录。
- `App.tsx`：加 `/login/callback` 公开路由。
- admin vite 代理加 `/api/auth → auth-svc:8002`。

## 6. is_platform_admin / 租户映射

- 登录用户落 `platform` 租户（admin 跨租户操作走 `admin_db_session`，已由 `is_platform_admin` 旁路 RLS）。
- 超管判定：`user_account.is_platform_admin` 布尔列（需加列 + 迁移）。**bootstrap**：env `BOOTSTRAP_ADMIN_DINGTALK_UNIONIDS=uid1,uid2`——callback 时若 unionId 命中，置该用户 `is_platform_admin=true`（仅 upsert 时设，不撤）。非命中且无现成超管 → 登录成功但 `is_platform_admin=false`（只读视角）。

## 7. 安全考量

- `state` 随机 + Redis 校验，防 CSRF / 授权码重放。
- JWT 用现有 `jwt_secret`（prod 已 fail-closed 断言，R2e）。access TTL 建议 8h，refresh rotation（同 portal）。
- 钉钉 `client_secret` 仅后端持有，前端永不接触。
- 回跳 URL 用白名单（仅 admin origin），防开放重定向。
- 前端不再可伪造 `is_platform_admin`——一切以后端 JWT payload 为准。

## 8. 配置（env，新增）

```
DINGTALK_CLIENT_ID=...
DINGTALK_CLIENT_SECRET=...
DINGTALK_CORP_ID=...              # 可选，取决于接口
DINGTALK_SSO_REDIRECT_URI=https://admin.apihub.internal/login/callback
BOOTSTRAP_ADMIN_DINGTALK_UNIONIDS=uid1,uid2
ADMIN_JWT_TTL_SECONDS=28800       # 8h
```

prod 经 external-secrets / KMS 注入（同 HMAC key 模式）。

## 9. 待用户提供（实现前必需）

1. 钉钉应用 **clientId / clientSecret**（与 notification 用的应用是否同一个？）。
2. **corpId**（若用企业内接口）。
3. 回跳 URI（dev：`http://localhost:5173/login/callback`；prod 域名）。
4. **初始超管 unionId 列表**（bootstrap）。

## 10. 范围外 / 后续

- portal 外部开发者登录不动（保留邮箱/密码）。
- 不做 RBAC 细粒度（仅 is_platform_admin 二值；角色细分留后续）。
- 不做多 IdP（仅钉钉；若将来要 OIDC 通用，抽象 provider 层）。
- admin API key（机器/脚本访问）仍保留 X-API-Key 路径，与浏览器 SSO 并存。

## 11. 实现拆分（供新会话 plan）

1. 后端 T1：`Settings` 加钉钉字段 + 迁移加 `user_account.is_platform_admin` 列。
2. T2：`identity.upsert_sso_user` + bootstrap 超管逻辑。
3. T3：两个 dingtalk 端点（authorize / callback）+ state Redis。
4. T4：单测（state 校验 / unionId→user / JWT payload 断言 is_platform_admin）。
5. 前端 T5：`client.ts` Bearer 切换 + refresh。
6. T6：`Login.tsx` 钉钉按钮 + `LoginCallback.tsx` + 路由 + vite `/api/auth` 代理。
7. T7：端到端 kind 验证（mock 钉钉或真实 corp 测试号）。

## 相关

- 现状审计：[[apihub-audit-2026-07-15]]（Login dev stub 列为 P2 安全项）
- 前端轮次：#78（Tailwind）/ #79（4 页）/ #80（代理）/ #81（CSV 导出）
- 钉钉集成先例：notification-svc dingtalk channel、工单审批流
