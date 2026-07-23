# Admin 控制台 SSO（钉钉 OAuth）

> 状态：已实现（feature 分支 `feat/admin-dingtalk-sso`）。替换 admin 控制台原 dev-stub 登录（前端伪造 `isPlatformAdmin`），改为钉钉 OAuth2 → 后端签发 JWT，身份以 JWT payload 为准。

## 1. 目标与边界

- **仅 admin 控制台**。portal 外部开发者登录（邮箱/密码）不动。
- admin 浏览器登录走钉钉 IdP；auth-svc 签发带 `is_platform_admin` 的 access JWT + refresh JWT；前端切到 `Authorization: Bearer`，不再伪造超管。
- **不触动数据面鉴权**：复用既有 Bearer-JWT 分流（`apihub_core.auth.authenticate_request` 已解码 `eyJ` token 回填 `TenantContext`），SSO 只新增「钉钉 OAuth → 签 JWT」链路。
- 机器/脚本访问仍可走 `X-API-Key`（与浏览器 SSO 并存）。
- 仅 `is_platform_admin` 二值（不做 RBAC 细粒度；仅钉钉单 IdP）。

## 2. 登录流程

```
浏览器            admin 前端                 auth-svc                 钉钉 OAuth
  | 点「钉钉登录」     |                          |                        |
  |----------------->| GET /v1/auth/dingtalk/authorize?redirect=<cb>     |
  |                  |  <── {authorize_url, state}（state 存 Redis 600s）|
  |<── 跳转钉钉扫码 ──────────────────────────────────────────────────>|
  | 扫码同意          |                          |                        |
  |── code+state 回跳 /login/callback ──────────────────────────────> |
  |                  | POST /v1/auth/dingtalk/callback {code,state}     |
  |                  |                          | 校验+消费 state         |
  |                  |                          | 换 userAccessToken ────>|
  |                  |                          | 取 unionId/nick <──────|
  |                  |                          | upsert user_account    |
  |                  |                          | 签 admin JWT + refresh |
  |                  | <── {access_token, refresh_token, user}          |
  |<── 存 JWT+user，进 /                                                    |
```

## 3. 后端契约（auth-svc）

公开端点（`skip_auth_paths`，靠 K8s NetworkPolicy / 反代限来源）：

- `GET /v1/auth/dingtalk/authorize?redirect=<admin 回跳 URI>`
  → `{"authorize_url": str, "state": str}`。`redirect` 经白名单校验；`state` 随机存 Redis `t:sso:state:{state}` TTL 600s。
- `POST /v1/auth/dingtalk/callback`
  body `{"code": str, "state": str}`
  → `AuthResponse { access_token, refresh_token, expires_in, user:{id,name,is_platform_admin,tenant_id} }`。
  流程：校验 state（一次性，校验后即删）→ `exchange_code_for_token` → `fetch_userinfo`（取 `unionId`/`nick`）→ `identity.upsert_sso_user` → 签 access JWT（`tenant_id=platform`，`is_platform_admin`，TTL `admin_jwt_ttl_seconds`=8h）+ refresh JWT（jti 存 Redis）。

钉钉 OAuth2 端点（真实）：authorize `https://login.dingtalk.com/oauth2/auth`；token `POST https://api.dingtalk.com/v1.0/oauth2/userAccessToken` `{clientId,clientSecret,grantType:"authorization_code",code}`；userinfo `GET https://api.dingtalk.com/v1.0/contact/users/me`（header `x-acs-dingtalk-access-token`）→ `accessToken` / `unionId` / `nick`。

refresh：admin 复用既有 `POST /v1/auth/refresh`（通用，按 refresh token 内 `tenant_id` 签发）。前端 `/api/auth/v1/auth/refresh`。

## 4. 前端（admin）

- `pages/Login.tsx`：「钉钉登录」按钮 → `GET /api/auth/v1/auth/dingtalk/authorize` → `window.location.href = authorize_url`。
- `pages/LoginCallback.tsx`（路由 `/login/callback`，**公开**）：取 `code/state` → `POST /api/auth/v1/auth/dingtalk/callback`（`skipAuth`）→ `setTokens` → `navigate('/')`。
- `api/client.ts`：鉴权 `X-API-Key` → `Authorization: Bearer <jwt>`；401 → refresh 一次 → 重试；失败 `clearAuth` + 跳 `/login`。`downloadCsv` 同步改 Bearer。
- vite 代理：`/api/auth → auth-svc:8002`。

## 5. 数据模型

`user_account` 加 3 列（迁移 `scripts/init-db/15-sso-user-account.sql`，幂等）：
- `sso_provider text`、`sso_union_id text`、`is_platform_admin boolean NOT NULL DEFAULT false`
- 部分唯一索引 `uq_user_sso(sso_provider, sso_union_id) WHERE sso_provider IS NOT NULL`
- `email` 保持 `UNIQUE NOT NULL`：SSO 用户合成 `<union_id>@<provider>.sso.local`。

**不落 `tenant_member`**：admin 是平台级全局身份，跨租户操作走 `admin_db_session`（旁路 RLS）；JWT 的 `tenant_id=platform` 仅为标签。

## 6. 超管判定（bootstrap）

`BOOTSTRAP_ADMIN_DINGTALK_UNIONIDS=uid1,uid2`：callback 时 `upsert_sso_user` 命中即置 `is_platform_admin=true`（**仅设不撤**）。未命中 → `is_platform_admin=false`（只读视角）。

⚠️ prod 首次部署前**必须**至少配一个超管 unionId，否则登录后无人可管。

## 7. 配置（env）

```
DINGTALK_CLIENT_ID=...                 # 空 → SSO 未启用（authorize/callback 返 503）
DINGTALK_CLIENT_SECRET=...             # 仅后端持有，prod 经 external-secrets/KMS 注入
DINGTALK_CORP_ID=...                   # 可选
DINGTALK_SSO_REDIRECT_URI=https://admin.apihub.internal/login/callback   # 须与钉钉应用回跳一致
DINGTALK_MOCK_MODE=false               # dev/kind=true：mock IdP，免真实钉钉应用
BOOTSTRAP_ADMIN_DINGTALK_UNIONIDS=uid1,uid2
ADMIN_JWT_TTL_SECONDS=28800            # 8h
```

凭据**可选**：未配置时 SSO 关闭，admin 仍可用 `X-API-Key` 机器访问。不纳入 `validate_security()` 的 `_INSECURE_DEFAULTS`（SSO 默认关）。

## 8. 安全

- **state CSRF**：随机 state 存 Redis，callback 校验后**立即删**（external call 之前）→ 重放 401。
- **开放重定向**：`redirect` 白名单（`localhost`/`127.0.0.1`/`*.apihub.internal`/与 `DINGTALK_SSO_REDIRECT_URI` 同 host）。
- **client_secret 仅后端**：前端永不接触。
- **身份不可伪造**：`is_platform_admin` 由后端 JWT payload 决定，前端只读。
- JWT 用既有 `jwt_secret`（prod fail-closed）。

## 9. 部署（prod）

1. apply 迁移 `scripts/init-db/15-sso-user-account.sql`（as owner `apihub`，幂等）。
2. external-secrets/KMS 注入 `DINGTALK_CLIENT_SECRET`（及 `DINGTALK_CLIENT_ID` 等）到 auth-svc。
3. 配 `BOOTSTRAP_ADMIN_DINGTALK_UNIONIDS`（至少 1 个超管）+ `DINGTALK_SSO_REDIRECT_URI`（与钉钉应用回跳一致）。
4. `DINGTALK_MOCK_MODE` 在 prod 保持 `false`。
5. 部署 auth-svc 新镜像 → admin 前端新构建。

## 10. 验证

- **单测**（`services/services/auth/tests/test_sso.py`，9 例）覆盖：bootstrap 解析、`upsert_sso_user`（首次创建/复用/bootstrap 置超管）、钉钉 OAuth 客户端（URL 构造 + mock 交换/userinfo）、端点全链（mock-mode：authorize 存 state / 坏 state 401 / callback 签发含 `is_platform_admin`+`tenant_id=platform` 的 JWT）。端点测试经 `httpx.ASGITransport` 跑全 app。
- **kind 全链 e2e（部署后）**：见 `scripts/k8s/sso-mock-e2e.sh`——部署 auth 新镜像到 kind（`DINGTALK_MOCK_MODE=true`）后，port-forward 跑 authorize→callback→JWT→admin 200 / 重放 state 401 / 非超管 unionId→false。**本会话未跑**（kind auth 仍跑旧镜像，需重建+重部署；逻辑已由单测覆盖）；作为部署闸门由部署方/后续会话执行。

## 11. 范围外 / 后续

- portal 登录不动。
- 不做 RBAC 细粒度（仅 `is_platform_admin` 二值）/ 不做多 IdP（仅钉钉）。
- admin refresh 经 `/v1/auth/refresh` 按 `jwt_ttl_seconds`(2h) 签发（非 admin 8h）——可接受；若要 8h 须在 `refresh_access` 按 `tenant_id=platform` 分流（follow-up）。
- 真实钉钉响应字段名（`accessToken`/`unionId`/`nick`）以真实 corp 测试为准；mock 路径已覆盖逻辑。
- `LoginCallback` 在 dev React StrictMode 下 `useEffect` 双触发 → 第二次 POST 因 `code` 一次性返错（prod 无 StrictMode 双触发，非问题；如需可在 callback 加 ref 去重）。

## 相关

- 设计 spec：`docs/superpowers/specs/2026-07-23-admin-dingtalk-sso-design.md`
- 实现计划：`docs/superpowers/plans/2026-07-24-admin-dingtalk-sso.md`
- 钉钉集成先例：notification-svc dingtalk channel（webhook 机器人，与本 SSO 的 OAuth 应用不同）
