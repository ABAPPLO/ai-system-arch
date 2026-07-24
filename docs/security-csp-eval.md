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
