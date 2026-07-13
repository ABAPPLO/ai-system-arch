# Phase 3 第四切片「JWT refresh token」设计

> 日期：2026-07-13
> 阶段：Phase 3 开放（`docs/10-roadmap.md` §5）第 4 个子项目
> 关联：`docs/superpowers/specs/2026-07-12-phase3-portal-identity-foundation-design.md`（身份地基）

## 1. Goal

当前 access token 有效期 2h，过期后用户必须重新登录。加 refresh token（7d）+ Portal 前端自动续期，开发者无需手动重新登录。

### 1.1 范围

- **apihub-core jwt_utils**：加 `issue_refresh_token()`
- **Settings**：加 `jwt_refresh_ttl_seconds`
- **auth 服务**：`POST /v1/auth/login` 响应加 `refresh_token` + 新增 `POST /v1/auth/refresh`
- **Redis**：refresh token 存储，支持吊销
- **portal-bff**：透传 `/v1/portal/auth/refresh`
- **Portal 前端**：`api/client.ts` 401 自动调 refresh 续期
- **端到端 smoke**：续期验证

### 1.2 非目标

- refresh token rotation 的旧 token 家族追踪（单次 rotation）
- 跨设备 session 管理
- 后台 admin 端 refresh token（admin 用 API Key，不需要）

## 2. 流程

```
POST /v1/auth/login
  → 签 access_token (2h) + refresh_token (7d, type=refresh, jti)
  → Redis: SET t:refresh:{jti} {user_id} EX 604800
  → 返回 { access_token, refresh_token, expires_in, user }

POST /v1/auth/refresh { refresh_token }
  → decode refresh_token → 检查 type=refresh
  → Redis: GET t:refresh:{jti} → 不存在则 401
  → Redis: DEL t:refresh:{jti} (rotation)
  → 签新 access_token + 新 refresh_token
  → Redis: SET t:refresh:{new_jti} {user_id} EX 604800
  → 返回 { access_token, refresh_token, expires_in }
```

## 3. apihub-core 变更

### 3.1 jwt_utils.py — 新增

```python
def issue_refresh_token(*, user_id: str, tenant_id: str, secret: str, ttl_seconds: int) -> str:
    import uuid
    payload = {
        "user_id": user_id, "tenant_id": tenant_id, "type": "refresh",
        "jti": uuid.uuid4().hex,
        "iat": int(time.time()), "exp": int(time.time()) + ttl_seconds,
    }
    return jwt.encode(payload, secret, algorithm=ALGORITHM)
```

### 3.2 config.py

```python
jwt_refresh_ttl_seconds: int = 604800  # 7天
```

## 4. auth 服务变更

### 4.1 identity.py

`login()` — 签发完 access_token 后追加 refresh_token + 写 Redis。

`refresh_access(refresh_token)` — 新函数：decode → 查 Redis → rotation → 返回新 token 对。

### 4.2 routes.py

新增 `/v1/auth/refresh` 端点（同 `/v1/auth/login` 模式）。

### 4.3 models.py

`AuthResponse` 加 `refresh_token: str` 和 `expires_in: int`。新增 `RefreshRequest(BaseModel)`。

## 5. portal-bff

`routes.py` — `/v1/portal/auth/refresh` 透传 auth（同 login 模式，已有 skip_auth_paths 覆盖）。

## 6. Portal 前端

### 6.1 api/client.ts

- 新增 `REFRESH_STORAGE = 'apihub_portal_refresh'`
- `setTokens(token, refreshToken, user)` 替代 `setAuth()`
- `request()` 401 处理：调 refresh → 成功则重试原请求 → 失败则跳登录

### 6.2 Login.tsx

`setAuth(r.access_token, r.user)` → `setTokens(r.access_token, r.refresh_token, r.user)`

## 7. 实现顺序

1. `jwt_utils.py` — `issue_refresh_token()`
2. `config.py` — `jwt_refresh_ttl_seconds`
3. `auth/models.py` — `RefreshRequest` + AuthResponse 加字段
4. `auth/identity.py` — login 加 refresh + `refresh_access()`
5. `auth/routes.py` — `/v1/auth/refresh`
6. portal-bff `routes.py` — 透传
7. Portal 前端 `api/client.ts` — refresh + `setTokens()`
8. Portal 前端 `Login.tsx` — 改 `setTokens()`
9. 端到端 smoke
10. lint
