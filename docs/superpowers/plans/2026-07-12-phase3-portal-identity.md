# Phase 3 第一切片「外部开发者身份地基」Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让外部开发者 注册→邮箱验证→登录→建应用→拿 API Key→用该 Key 经 APISIX 调通一个 `visibility=public` 的 API（200）。

**Architecture:** auth 服务扩展 3 个身份端点 + JWT 签发；`apihub_core.authenticate_request` 加 JWT 分流（token 以 `eyJ` 开头本地验签，否则原 API-Key 流程）；dispatcher 转发前查 `api.visibility`（public/tenant/private）授权；新 portal-bff 服务（镜像 admin）做薄代理 + app/key 直写表；新 Portal 前端（镜像 frontend/admin，JWT 存 localStorage）。全复用现有表，不建新表。

**Tech Stack:** FastAPI + asyncpg + Redis（apihub_core）+ PyJWT；React + Vite + TS + Tailwind + Zustand（Portal 前端，同 frontend/admin）。

**Spec:** `docs/superpowers/specs/2026-07-12-phase3-portal-identity-foundation-design.md`

## Global Constraints

- 全复用现有表：`user_account`(schema:32) / `tenant_member`(:47) / `app`(:62) / `api_key`(:76) / `api.visibility`(:109)。**不建新表**，不改 schema。
- JWT 用 `Settings.jwt_secret`（若不存在则加，见 Task 1）；access token TTL 7200s(2h)；HS256。
- 邮箱验证 token 存 Redis：`t:verify:{token}` → user_id，TTL 86400s(24h)。dev stub：token 写日志 + 响应返回（不真发邮件）。
- 人认证 = JWT（`Authorization: Bearer`）；机器调用 = 应用 API Key（`X-API-Key`，经 APISIX）。dispatcher visibility 检查用 `get_tenant_context()`（已由 middleware 设置）。
- Portal 前端 JWT 存 `localStorage['apihub_portal_token']` + `localStorage['apihub_portal_user']`。
- 端口：portal-bff = 8011（8010 是 workflow-svc）；Portal 前端 dev = 5174（5173 是 admin）。
- 分支 `feat/phase3-portal-identity`（已建）；每 Task 末 commit；push/PR 等发话。
- 实名仅 `basic`（status active 即邮箱已验证）；`enterprise` defer。
- 调通靶子：seed 的 `smoke-sync` API 标 `visibility=public`。
- TDD：每步先写测试看 fail → 实现 → 看 pass → commit。`asyncio_mode=auto`。

## File Structure

**Create:**
- `services/libs/apihub-core/src/apihub_core/jwt_utils.py` — JWT 签发/验签纯函数
- `services/services/auth/src/auth/identity.py` — 注册/验证/登录业务（user_account + tenant_member + Redis verify token）
- `services/services/portal/` — 新 BFF 服务：`pyproject.toml`、`src/portal/{__init__,main,routes,models,repository}.py`、`tests/{conftest,test_routes}.py`、`Dockerfile`
- `frontend/portal/` — 新前端：`package.json`、`vite.config.ts`、`tsconfig.json`、`index.html`、`src/{main.tsx,App.tsx,index.css,api/client.ts,pages/{Register,Login,Apps}.tsx,store.ts}`
- `scripts/smoke/portal-onboarding.py` — 端到端 smoke
- `deploy/k8s/base/services/portal/{deployment,service}.yaml` — portal-bff k8s 清单

**Modify:**
- `services/libs/apihub-core/src/apihub_core/auth.py` — `authenticate_request` 加 JWT 分流
- `services/libs/apihub-core/src/apihub_core/config.py` — 加 `jwt_secret` / `jwt_ttl_seconds` 字段（若缺）
- `services/libs/apihub-core/src/apihub_core/errors.py` — 加 `CONFLICT`/`FORBIDDEN`/`INVALID_INPUT` ErrorCode（若缺）
- `services/services/auth/src/auth/{routes,models,main}.py` — 加 register/verify-email/login 端点
- `services/services/dispatcher/src/dispatcher/{models,resolver,routes}.py` — visibility 字段 + 检查
- `Makefile` — `run-portal` / `portal-frontend-*` / `run-portal-frontend` targets
- `scripts/init-db/02-seed.sql` — smoke-sync API 标 `visibility=public`

---

### Task 1: apihub_core JWT 支持（jwt_utils + authenticate_request 分流）

**Files:**
- Create: `services/libs/apihub-core/src/apihub_core/jwt_utils.py`
- Modify: `services/libs/apihub-core/src/apihub_core/config.py`（加 jwt_secret/jwt_ttl_seconds 若缺）
- Modify: `services/libs/apihub-core/src/apihub_core/auth.py:17-67`（authenticate_request 加 JWT 分流）
- Test: `services/libs/apihub-core/tests/test_jwt_utils.py`

**Interfaces:**
- Produces: `jwt_utils.issue_token(user_id, tenant_id, *, is_platform_admin, secret, ttl_seconds) -> str`；`jwt_utils.decode_token(token, secret) -> dict`（含 user_id/tenant_id/is_platform_admin/exp）；`jwt_utils.is_jwt(token) -> bool`（token 以 `eyJ` 开头）。`authenticate_request` 签名不变，内部对 JWT token 本地验签构造 TenantContext。

- [ ] **Step 1: 确认 Settings 字段**

Run: `grep -nE "jwt_secret|jwt_ttl|JWT_SECRET" services/libs/apihub-core/src/apihub_core/config.py`
Expected: 若无 `jwt_secret` 字段则 Step 2 加；若有则跳过字段添加。

- [ ] **Step 2: config.py 加字段（若缺）**

在 `Settings` 类适当位置加（若已存在 `jwt_secret` 则只加 ttl）：
```python
    jwt_secret: str = "dev-only-insecure-secret"  # prod 必须用强密钥（env 注入）
    jwt_ttl_seconds: int = 7200  # access token 2h
```

- [ ] **Step 3: 写失败测试 `test_jwt_utils.py`**

```python
"""jwt_utils 单测 —— 签发/验签/格式判定。"""

import pytest

from apihub_core import jwt_utils


def test_issue_and_decode_roundtrip():
    token = jwt_utils.issue_token(
        user_id="u_1", tenant_id="external-public", secret="s", ttl_seconds=60
    )
    assert jwt_utils.is_jwt(token) is True
    payload = jwt_utils.decode_token(token, "s")
    assert payload["user_id"] == "u_1"
    assert payload["tenant_id"] == "external-public"
    assert payload["is_platform_admin"] is False


def test_decode_wrong_secret_raises():
    token = jwt_utils.issue_token(user_id="u", tenant_id="t", secret="s", ttl_seconds=60)
    with pytest.raises(jwt_utils.JWTError):
        jwt_utils.decode_token(token, "other-secret")


def test_is_jwt_false_for_apikey():
    assert jwt_utils.is_jwt("ak_abcdef123456") is False
    assert jwt_utils.is_jwt("") is False
```

- [ ] **Step 4: 运行测试看 fail**

Run: `pytest services/libs/apihub-core/tests/test_jwt_utils.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'apihub_core.jwt_utils'`

- [ ] **Step 5: 实现 `jwt_utils.py`**

```python
"""JWT 签发/验签 —— 外部开发者「人」的登录态（HS256）。

与 API Key（机器凭证）分离：JWT 代表开发者这个人，TTL 2h。
"""

import time

import jwt

ALGORITHM = "HS256"


class JWTError(Exception):
    """JWT 验签/解码失败。"""


def is_jwt(token: str) -> bool:
    """粗判：JWT 第一段 base64url 以 'eyJ' 开头。"""
    return bool(token) and token.startswith("eyJ")


def issue_token(
    *,
    user_id: str,
    tenant_id: str,
    secret: str,
    ttl_seconds: int,
    is_platform_admin: bool = False,
) -> str:
    payload = {
        "user_id": user_id,
        "tenant_id": tenant_id,
        "is_platform_admin": is_platform_admin,
        "iat": int(time.time()),
        "exp": int(time.time()) + ttl_seconds,
    }
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def decode_token(token: str, secret: str) -> dict:
    try:
        return jwt.decode(token, secret, algorithms=[ALGORITHM])
    except jwt.PyJWTError as e:
        raise JWTError(str(e)) from e
```

- [ ] **Step 6: 确认依赖 PyJWT**

Run: `grep -n "pyjwt\|PyJWT" services/libs/apihub-core/pyproject.toml`
若无则加 `dependencies`：`"PyJWT>=2.8,<3"`，并 `cd services/libs/apihub-core && pip install -e .`。

- [ ] **Step 7: 运行测试看 pass**

Run: `pytest services/libs/apihub-core/tests/test_jwt_utils.py -v`
Expected: 3 passed。

- [ ] **Step 8: `auth.py::authenticate_request` 加 JWT 分流**

在 `authenticate_request` 函数体 `if not api_key: raise ...` 之后、httpx 调 auth verify 之前插入：
```python
    # JWT 分流：外部开发者「人」的 token（eyJ 开头）本地验签，
    # 不走 auth /v1/apikey/verify（那是机器 API Key 流程）。
    from apihub_core import jwt_utils

    if jwt_utils.is_jwt(api_key):
        try:
            payload = jwt_utils.decode_token(api_key, settings.jwt_secret)
        except jwt_utils.JWTError:
            raise ApiError(ErrorCode.UNAUTHORIZED, "invalid or expired token")
        ctx = TenantContext(
            tenant_id=payload["tenant_id"],
            tenant_type="external",
            user_id=payload["user_id"],
            is_platform_admin=payload.get("is_platform_admin", False),
        )
        set_tenant_context(ctx)
        return ctx
    # 否则：原 API Key 流程（以下 httpx 调 auth verify 代码不变）
```

- [ ] **Step 9: 回归 apihub_core 既有测试**

Run: `pytest services/libs/apihub-core/tests/ -v`
Expected: 全 pass（含既有 test_tenant 等，无新 fail）。

- [ ] **Step 10: Commit**

```bash
git add services/libs/apihub-core/src/apihub_core/jwt_utils.py \
        services/libs/apihub-core/src/apihub_core/config.py \
        services/libs/apihub-core/src/apihub_core/auth.py \
        services/libs/apihub-core/tests/test_jwt_utils.py \
        services/libs/apihub-core/pyproject.toml
git commit -m "feat(apihub-core): JWT 签发/验签 + authenticate_request JWT 分流（人认证）"
```

---

### Task 2: auth 身份端点（register / verify-email / login）

**Files:**
- Create: `services/services/auth/src/auth/identity.py`
- Modify: `services/services/auth/src/auth/routes.py`（register_routes 内加 3 端点）
- Modify: `services/services/auth/src/auth/models.py`（加 RegisterRequest/LoginRequest/AuthResponse）
- Modify: `services/services/auth/src/auth/main.py:12-19`（skip_auth_paths 加 3 个 /v1/auth/*）
- Test: `services/services/auth/tests/test_identity.py`

**Interfaces:**
- Consumes: `jwt_utils.issue_token`（Task 1）、`db.admin_db_session`/`db_session`、`redis`、`Settings.jwt_secret/jwt_ttl_seconds`。
- Produces: `POST /v1/auth/register {email,password,phone,name}` → 201 + `{verify_token}`（dev stub）；`GET /v1/auth/verify-email?token=...` → 200 + `{user_id, status:active, tenant_id}`；`POST /v1/auth/login {email,password}` → 200 + `{access_token, user:{id,name,tenant_id}}`。业务函数 `identity.create_user/verify_email/login`。

- [ ] **Step 1: 确认 ErrorCode 齐全**

Run: `grep -nE "CONFLICT|FORBIDDEN|INVALID_INPUT" services/libs/apihub-core/src/apihub_core/errors.py`
缺则在 `ErrorCode` 枚举补（如 `CONFLICT=10008`、`FORBIDDEN=10009`、`INVALID_INPUT=10010`），并在 handler 映射 http_status（409/403/400）。

- [ ] **Step 2: 写失败测试 `test_identity.py`**

```python
"""auth 身份业务单测（fake_redis + dev 栈 PG）。需 make dev-up。"""

import pytest

from auth import identity


@pytest.mark.asyncio
async def test_register_creates_pending_user(fake_redis):
    user = await identity.create_user(
        email="new@example.com", password="secret123", phone="13800000000", name="New"
    )
    assert user["status"] == "pending"
    assert user["verification_level"] == "email"
    assert await fake_redis.get(f"t:verify:{user['verify_token']}") == user["user_id"]


@pytest.mark.asyncio
async def test_register_duplicate_email_raises(fake_redis):
    await identity.create_user(
        email="dup@example.com", password="secret123", phone="138", name="A"
    )
    from apihub_core.errors import ApiError

    with pytest.raises(ApiError):
        await identity.create_user(
            email="dup@example.com", password="secret123", phone="139", name="B"
        )


@pytest.mark.asyncio
async def test_verify_email_activates_and_joins_external_public(fake_redis):
    user = await identity.create_user(
        email="v@example.com", password="secret123", phone="138", name="V"
    )
    result = await identity.verify_email(user["verify_token"])
    assert result["status"] == "active"
    assert result["tenant_id"] == "external-public"


@pytest.mark.asyncio
async def test_login_unverified_raises(fake_redis):
    await identity.create_user(
        email="l@example.com", password="secret123", phone="138", name="L"
    )
    from apihub_core.errors import ApiError

    with pytest.raises(ApiError):
        await identity.login(email="l@example.com", password="secret123")
```

- [ ] **Step 3: 运行看 fail**

Run: `pytest services/services/auth/tests/test_identity.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'auth.identity'`

- [ ] **Step 4: 实现 `identity.py`**

```python
"""外部开发者身份业务 —— 注册 / 邮箱验证 / 登录。

复用现有表：user_account(schema:32) + tenant_member(:47)。
邮箱验证 token 存 Redis（dev stub 不真发邮件）。
"""

import secrets

import bcrypt
from apihub_core import db, redis
from apihub_core.errors import ApiError, ErrorCode
from apihub_core.logging import get_logger

log = get_logger(__name__)

EXTERNAL_PUBLIC_TENANT = "external-public"
VERIFY_TTL = 86400  # 24h


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _check_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


async def create_user(*, email: str, password: str, phone: str, name: str) -> dict:
    """注册：写 user_account(pending) + Redis 验证 token。重复邮箱 → 409。"""
    async with db.admin_db_session() as conn:
        exists = await conn.fetchval(
            "SELECT id FROM user_account WHERE email = $1", email
        )
        if exists:
            raise ApiError(ErrorCode.CONFLICT, "email already registered", http_status=409)

        user_id = f"u_{secrets.token_hex(8)}"
        await conn.execute(
            """
            INSERT INTO user_account (id, email, phone, password_hash, name,
                                       verification_level, status)
            VALUES ($1, $2, $3, $4, $5, 'email', 'pending')
            """,
            user_id, email, phone, _hash_password(password), name,
        )

    verify_token = secrets.token_urlsafe(32)
    await redis.t_set(f"t:verify:{verify_token}", user_id, ex=VERIFY_TTL)
    log.info("user_registered", user_id=user_id, email=email)
    return {"user_id": user_id, "status": "pending",
            "verification_level": "email", "verify_token": verify_token}


async def verify_email(token: str) -> dict:
    """验证邮箱：标 status=active + 加 tenant_member(external-public)。"""
    user_id = await redis.t_get(f"t:verify:{token}")
    if not user_id:
        raise ApiError(ErrorCode.INVALID_INPUT, "invalid or expired token", http_status=400)

    async with db.admin_db_session() as conn:
        row = await conn.fetchrow(
            "UPDATE user_account SET status='active', last_login_at=NOW() "
            "WHERE id=$1 RETURNING id, name",
            user_id,
        )
        if not row:
            raise ApiError(ErrorCode.NOT_FOUND, "user not found", http_status=404)
        await conn.execute(
            """
            INSERT INTO tenant_member (id, tenant_id, user_id, role)
            VALUES ($1, $2, $3, 'developer')
            ON CONFLICT (tenant_id, user_id) DO NOTHING
            """,
            f"tm_{secrets.token_hex(8)}", EXTERNAL_PUBLIC_TENANT, user_id,
        )
    await redis.t_delete(f"t:verify:{token}")
    log.info("email_verified", user_id=user_id)
    return {"user_id": user_id, "name": row["name"], "status": "active",
            "tenant_id": EXTERNAL_PUBLIC_TENANT}


async def login(*, email: str, password: str) -> dict:
    """登录：bcrypt 校验 + status=active 检查 → 签 JWT。"""
    from apihub_core import jwt_utils
    from apihub_core.config import get_settings

    async with db.admin_db_session() as conn:
        row = await conn.fetchrow(
            "SELECT id, email, name, password_hash, status FROM user_account WHERE email=$1",
            email,
        )
    if not row or not row["password_hash"] or not _check_password(password, row["password_hash"]):
        raise ApiError(ErrorCode.UNAUTHORIZED, "invalid email or password", http_status=401)
    if row["status"] != "active":
        raise ApiError(ErrorCode.FORBIDDEN, "email not verified", http_status=403)

    s = get_settings()
    token = jwt_utils.issue_token(
        user_id=row["id"], tenant_id=EXTERNAL_PUBLIC_TENANT,
        secret=s.jwt_secret, ttl_seconds=s.jwt_ttl_seconds,
    )
    return {"access_token": token,
            "user": {"id": row["id"], "name": row["name"], "tenant_id": EXTERNAL_PUBLIC_TENANT}}
```

- [ ] **Step 5: 加 models（RegisterRequest/LoginRequest/AuthResponse）**

在 `auth/models.py` 末尾加（确保 `EmailStr`、`Field`、`BaseModel`、`datetime` 已导入）：
```python
class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    phone: str = Field(min_length=4, max_length=20)
    name: str = Field(min_length=1, max_length=64)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class AuthResponse(BaseModel):
    access_token: str
    user: dict
```

- [ ] **Step 6: routes.py 加 3 端点**

在 `register_routes` 内（`/v1/auth/health` 前）加；顶部 import 加 `RegisterRequest, LoginRequest, AuthResponse`：
```python
    @app.post("/v1/auth/register", status_code=201)
    async def register(payload: RegisterRequest):
        from auth import identity
        return await identity.create_user(
            email=payload.email, password=payload.password,
            phone=payload.phone, name=payload.name,
        )

    @app.get("/v1/auth/verify-email")
    async def verify_email_endpoint(token: str):
        from auth import identity
        return await identity.verify_email(token)

    @app.post("/v1/auth/login", response_model=AuthResponse)
    async def login_endpoint(payload: LoginRequest):
        from auth import identity
        return await identity.login(email=payload.email, password=payload.password)
```

- [ ] **Step 7: main.py 扩 skip_auth_paths**

`auth/main.py` 的 `skip_auth_paths` 加：
```python
        "/v1/auth/register",
        "/v1/auth/verify-email",
        "/v1/auth/login",
```

- [ ] **Step 8: auth tests conftest 加 fake_redis（若无）**

确认 `services/services/auth/tests/conftest.py` 有 `fake_redis`（镜像 admin conftest:36-42）；DB-touching 测试需 `make dev-up`（同项目惯例，asyncpg 直连真 PG）。

- [ ] **Step 9: 运行测试（需 dev 栈）**

```bash
make dev-up
pytest services/services/auth/tests/test_identity.py -v
```
Expected: 4 passed。

- [ ] **Step 10: ruff/mypy + commit**

```bash
ruff check services/services/auth/ services/libs/apihub-core/
mypy services/services/auth/ services/libs/apihub-core/
git add services/services/auth/ services/libs/apihub-core/src/apihub_core/errors.py
git commit -m "feat(auth): 外部开发者注册/邮箱验证/登录 + JWT 签发（identity 模块）"
```

---

### Task 3: dispatcher visibility 授权检查

**Files:**
- Modify: `services/libs/apihub-core/src/apihub_core/db.py`（加 `meta_db_session`，绕 RLS 供 resolver 跨租户查元数据 —— **pre-flight 修正**：api/api_version 的 RLS 纯 tenant 过滤 `01-schema.sql:290-304`，resolver 用 db_session 会挡掉跨租户 public API）
- Modify: `services/services/dispatcher/src/dispatcher/models.py`（`ApiVersionSnapshot` 加 `visibility`）
- Modify: `services/services/dispatcher/src/dispatcher/resolver.py:43-67,70-76,105-124`（`_get_base_path` → `_get_api_meta` 取 base_path+visibility；resolve 出口填 visibility）
- Modify: `services/services/dispatcher/src/dispatcher/routes.py:64-89`（dispatch 加检查）
- Create: `services/services/dispatcher/src/dispatcher/visibility.py`
- Test: `services/services/dispatcher/tests/test_visibility.py`

**Interfaces:**
- Consumes: `get_tenant_context()`。
- Produces: `ApiVersionSnapshot.visibility: str`；`visibility.check_visibility(snap, ctx) -> None`。public 放行；tenant 要求 `ctx.tenant_id == snap.tenant_id`；private 要求同租户 + `is_platform_admin`，否则 ApiError 403。

- [ ] **Step 1: 写失败测试 `test_visibility.py`**

```python
"""dispatcher visibility 三级授权单测（纯函数，不依赖 DB）。"""

import pytest

from apihub_core.errors import ApiError
from apihub_core.tenant import TenantContext

from dispatcher.visibility import check_visibility


def _snap(visibility, tenant_id="tenant_a"):
    return type("S", (), {"visibility": visibility, "tenant_id": tenant_id})()


def test_public_allows_any_tenant():
    ctx = TenantContext(tenant_id="external-public", tenant_type="external")
    check_visibility(_snap("public", "tenant_a"), ctx)  # 不 raise


def test_tenant_blocks_other_tenant():
    ctx = TenantContext(tenant_id="external-public", tenant_type="external")
    with pytest.raises(ApiError) as exc:
        check_visibility(_snap("tenant", "tenant_a"), ctx)
    assert exc.value.http_status == 403


def test_tenant_allows_same_tenant():
    ctx = TenantContext(tenant_id="tenant_a", tenant_type="internal")
    check_visibility(_snap("tenant", "tenant_a"), ctx)


def test_private_requires_platform_admin():
    ctx = TenantContext(tenant_id="tenant_a", tenant_type="internal", is_platform_admin=False)
    with pytest.raises(ApiError):
        check_visibility(_snap("private", "tenant_a"), ctx)
    from dataclasses import replace
    check_visibility(_snap("private", "tenant_a"), replace(ctx, is_platform_admin=True))
```

- [ ] **Step 2: 运行看 fail**

Run: `pytest services/services/dispatcher/tests/test_visibility.py -v`
Expected: FAIL — `No module named 'dispatcher.visibility'`

- [ ] **Step 3: 创建 `dispatcher/visibility.py`**

```python
"""visibility 授权 —— dispatcher 转发前检查 api.visibility。

public: 任何有效 caller（含 external-public）。
tenant: 仅同租户。
private: 同租户 + 平台超管。
"""

from apihub_core.errors import ApiError, ErrorCode
from apihub_core.tenant import TenantContext


def check_visibility(snap, ctx: TenantContext) -> None:
    visibility = getattr(snap, "visibility", "private")
    api_tenant = snap.tenant_id

    if visibility == "public":
        return
    if visibility == "tenant":
        if ctx.tenant_id != api_tenant:
            raise ApiError(ErrorCode.FORBIDDEN, "api not visible to this tenant", http_status=403)
        return
    # private
    if ctx.tenant_id != api_tenant or not ctx.is_platform_admin:
        raise ApiError(ErrorCode.FORBIDDEN, "api is private", http_status=403)
```

- [ ] **Step 4: 运行看 pass**

Run: `pytest services/services/dispatcher/tests/test_visibility.py -v`
Expected: 4 passed。

- [ ] **Step 5: `ApiVersionSnapshot` 加 visibility 字段**

`models.py` 的 `ApiVersionSnapshot` dataclass 加 `visibility: str = "private"`。

- [ ] **Step 6a: apihub_core/db.py 加 `meta_db_session`**

resolver 是平台网关职责（路由解析），需跨租户读 published API 元数据，再由 check_visibility 应用层授权。`db_session`（租户感知 RLS）会挡掉跨租户 public API（`01-schema.sql:290-304` 无 public 例外）。故加专用 `meta_db_session`（绕 RLS，不写审计，区别于 admin_db_session 的人工审计语义）。在 `db.py` 的 `admin_db_session` 之后加：
```python
@asynccontextmanager
async def meta_db_session() -> AsyncIterator[asyncpg.Connection]:
    """平台元数据查询会话 —— 绕过 RLS，可见所有租户元数据。

    仅供平台网关职责（如 dispatcher 路由解析）跨租户查 published API/api_version
    元数据，授权由应用层（dispatcher visibility 检查）做。不写审计（区别于
    admin_db_session 的人工审计场景）。业务代码禁用。
    """
    if _pool is None:
        raise RuntimeError("DB pool not initialized. Call init_pool first.")
    async with _pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            await conn.execute("SET LOCAL app.is_platform_admin = 'true'")
            yield conn
            await tr.commit()
        except Exception:
            await tr.rollback()
            raise
```

- [ ] **Step 6b: resolver.py 改用 `meta_db_session` + 取 visibility**

把 `_get_base_path` 改为 `_get_api_meta` 返回 `(base_path, visibility)`，**用 `meta_db_session`**（跨租户可见 public api）：
```python
async def _get_api_meta(api_id: str) -> tuple[str | None, str]:
    from apihub_core import db as _db
    async with _db.meta_db_session() as conn:
        row = await conn.fetchrow(
            "SELECT base_path, visibility FROM api WHERE id = $1", api_id
        )
    if not row:
        return None, "private"
    return row["base_path"], row["visibility"]
```
`resolve_by_path` 与 `resolve_by_header` 里的 `async with db.db_session() as conn:` 全部改为 `async with db.meta_db_session() as conn:`（跨租户 resolve）。`resolve_by_path` 的 `for row in rows:` 循环改为：
```python
    for row in rows:
        api_base, visibility = await _get_api_meta(row["api_id"])
        if not api_base:
            continue
        full_pattern = f"{api_base}{row['path']}"
        if _match_path(full_pattern, full_path):
            snap = _from_row(row)
            snap.visibility = visibility
            return snap
```
`resolve_by_header` 在 `snapshot = _from_row(row)` 后、return 前补：
```python
        _, visibility = await _get_api_meta(snapshot.api_id)
        snapshot.visibility = visibility
```
（缓存分支 `_from_json` 后也补一次 `_get_api_meta`，或把 visibility 纳入缓存 payload——后者更省，implementer 选一。）

- [ ] **Step 7: routes.py dispatch 加检查**

`dispatch` 内 `snap = await resolve_*(...)` 之后、`if snap.backend_type == "async_task"` 之前插入：
```python
        from apihub_core.tenant import get_tenant_context
        from dispatcher.visibility import check_visibility

        ctx = get_tenant_context()
        if ctx is not None:
            check_visibility(snap, ctx)
```

- [ ] **Step 8: 回归 dispatcher 既有测试**

Run: `pytest services/services/dispatcher/tests/ -v`
Expected: 全 pass（既有 async/workflow smoke 的 tenant_a API 对 tenant_a caller 仍放行）。

- [ ] **Step 9: Commit**

```bash
git add services/services/dispatcher/ services/libs/apihub-core/src/apihub_core/db.py services/libs/apihub-core/src/apihub_core/errors.py
git commit -m "feat(dispatcher): api.visibility 三级授权 + meta_db_session（跨租户 resolve public）"
```

---

### Task 4: portal-bff 新服务（镜像 admin）

**Files:**
- Create: `services/services/portal/{pyproject.toml,src/portal/{__init__,main,routes,models,repository}.py,tests/{conftest,test_routes}.py,Dockerfile}`
- Test: `services/services/portal/tests/test_routes.py`

**Interfaces:**
- Consumes: `create_app`、`db_session`、`require_tenant`、`auth.apikey.generate_api_key`。
- Produces: 服务 `portal`，端口 8011。`/v1/portal/auth/{register,verify-email,login}`（转发 auth）+ `GET/POST /v1/portal/apps` + `POST /v1/portal/apps/{id}/api-keys`（直写表）。

- [ ] **Step 1: pyproject.toml（镜像 admin）**

Run: `cat services/services/admin/pyproject.toml` → 复制结构，改 `name="portal"`、dependencies 含 `apihub-core`。

- [ ] **Step 2: `src/portal/__init__.py`** 空文件。

- [ ] **Step 3: `src/portal/main.py`**

```python
"""portal-bff 启动入口 —— 外部开发者门户聚合层（薄代理 + app/key 自助）。"""

from apihub_core import create_app

from portal.routes import register_routes

app = create_app(
    service_name="portal",
    build_routes=register_routes,
    skip_auth_paths=(
        "/health",
        "/metrics",
        "/v1/portal/auth/register",
        "/v1/portal/auth/verify-email",
        "/v1/portal/auth/login",
        "/docs",
        "/openapi.json",
    ),
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("portal.main:app", host="0.0.0.0", port=8011, workers=2, log_level="info")
```

- [ ] **Step 4: `src/portal/models.py`**

```python
from pydantic import BaseModel, Field


class AppCreate(BaseModel):
    name: str = Field(min_length=2, max_length=64)
    type: str = "external"


class AppResponse(BaseModel):
    id: str
    name: str
    tenant_id: str
    type: str
    status: str


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=2, max_length=64)


class ApiKeyResponse(BaseModel):
    id: str
    app_id: str
    name: str
    key_prefix: str
    api_key: str  # 明文仅此次返回
```

- [ ] **Step 5: `src/portal/repository.py`（直写 app/api_key 表）**

```python
"""portal app/key 自助 —— 直写 app/api_key 表（RLS 按 caller tenant 隔离）。

不复用 auth /v1/apps 端点：那个走 X-API-Key middleware，而 Portal 是 JWT 人认证。
"""

import secrets

from apihub_core import db


async def create_app_for_user(*, tenant_id: str, name: str, app_type: str) -> dict:
    app_id = f"app_{secrets.token_hex(8)}"
    async with db.db_session() as conn:
        await conn.execute(
            """
            INSERT INTO app (id, tenant_id, name, type, status)
            VALUES ($1, $2, $3, $4, 'active')
            """,
            app_id, tenant_id, name, app_type,
        )
    return {"id": app_id, "name": name, "tenant_id": tenant_id,
            "type": app_type, "status": "active"}


async def list_apps_for_user(*, tenant_id: str) -> list[dict]:
    async with db.db_session() as conn:
        rows = await conn.fetch(
            "SELECT id, name, tenant_id, type, status FROM app "
            "WHERE tenant_id=$1 ORDER BY created_at DESC",
            tenant_id,
        )
    return [dict(r) for r in rows]


async def create_api_key_for_app(*, tenant_id: str, app_id: str, name: str) -> dict:
    from auth.apikey import generate_api_key  # 复用 auth 的 key 生成纯函数

    plaintext, key_hash, display_prefix = generate_api_key()
    key_id = f"key_{secrets.token_hex(8)}"
    async with db.db_session() as conn:
        await conn.execute(
            """
            INSERT INTO api_key (id, tenant_id, app_id, key_prefix, key_hash, name, status)
            VALUES ($1, $2, $3, $4, $5, $6, 'active')
            """,
            key_id, tenant_id, app_id, display_prefix, key_hash, name,
        )
    return {"id": key_id, "app_id": app_id, "name": name,
            "key_prefix": display_prefix, "api_key": plaintext}
```

- [ ] **Step 6: `src/portal/routes.py`**

```python
"""portal-bff 路由 —— 身份端点转发 auth + app/key 自助。"""

import httpx
from apihub_core.config import get_settings
from apihub_core.errors import ApiError, ErrorCode
from apihub_core.logging import get_logger
from apihub_core.tenant import require_tenant
from fastapi import FastAPI

from portal import repository
from portal.models import ApiKeyCreate, ApiKeyResponse, AppCreate, AppResponse

log = get_logger(__name__)


def register_routes(app: FastAPI) -> None:
    settings = get_settings()
    # auth_service_url 形如 http://auth.apihub-system/v1/apikey/verify → 砍到 base
    auth_base = settings.auth_service_url.rsplit("/", 2)[0]

    async def _forward(method: str, path: str, **kw) -> tuple[int, dict]:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.request(method, f"{auth_base}{path}", **kw)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {"raw": r.text[:200]}

    # ========== 身份端点（转发 auth，无需 JWT）==========
    @app.post("/v1/portal/auth/register", status_code=201)
    async def register(payload: dict):
        st, body = await _forward("POST", "/v1/auth/register", json=payload)
        if st >= 400:
            raise ApiError(ErrorCode.INTERNAL, f"auth error: {body}", http_status=st)
        return body

    @app.get("/v1/portal/auth/verify-email")
    async def verify_email(token: str):
        st, body = await _forward("GET", "/v1/auth/verify-email", params={"token": token})
        if st >= 400:
            raise ApiError(ErrorCode.INTERNAL, f"auth error: {body}", http_status=st)
        return body

    @app.post("/v1/portal/auth/login")
    async def login(payload: dict):
        st, body = await _forward("POST", "/v1/auth/login", json=payload)
        if st >= 400:
            raise ApiError(ErrorCode.UNAUTHORIZED, "invalid credentials", http_status=st)
        return body

    # ========== app/key 自助（需 JWT → require_tenant）==========
    @app.post("/v1/portal/apps", response_model=AppResponse, status_code=201)
    async def create_app(payload: AppCreate):
        ctx = require_tenant()
        return await repository.create_app_for_user(
            tenant_id=ctx.tenant_id, name=payload.name, app_type=payload.type
        )

    @app.get("/v1/portal/apps", response_model=list[AppResponse])
    async def list_apps():
        ctx = require_tenant()
        return await repository.list_apps_for_user(tenant_id=ctx.tenant_id)

    @app.post("/v1/portal/apps/{app_id}/api-keys", response_model=ApiKeyResponse, status_code=201)
    async def create_api_key(app_id: str, payload: ApiKeyCreate):
        ctx = require_tenant()
        return await repository.create_api_key_for_app(
            tenant_id=ctx.tenant_id, app_id=app_id, name=payload.name
        )
```

- [ ] **Step 7: `tests/conftest.py`（镜像 admin conftest，client 带 Bearer）**

照抄 `services/services/admin/tests/conftest.py` 的 `_ENV_DEFAULTS`/`reset_state`/`fake_redis`，`client` fixture 改：
```python
@pytest.fixture
def client(monkeypatch):
    from portal.main import app
    from httpx import ASGITransport, AsyncClient
    from apihub_core.tenant import TenantContext, set_tenant_context
    from apihub_core import auth as core_auth

    async def _jwt_auth(request, settings, api_key, required_scopes=None):
        ctx = TenantContext(tenant_id="external-public", tenant_type="external", user_id="u_test")
        set_tenant_context(ctx)
        return ctx
    monkeypatch.setattr(core_auth, "authenticate_request", _jwt_auth)

    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer eyJ.test.token"},
    )
```

- [ ] **Step 8: 写 `tests/test_routes.py`**

```python
"""portal-bff 路由单测（mock repository）。"""


async def test_create_app_uses_caller_tenant(client, monkeypatch):
    captured = {}

    async def fake_create(*, tenant_id, name, app_type):
        captured["tenant_id"] = tenant_id
        return {"id": "app_x", "name": name, "tenant_id": tenant_id,
                "type": app_type, "status": "active"}
    monkeypatch.setattr("portal.routes.repository.create_app_for_user", fake_create)

    r = await client.post("/v1/portal/apps", json={"name": "my app", "type": "external"})
    assert r.status_code == 201
    assert captured["tenant_id"] == "external-public"


async def test_list_apps(client, monkeypatch):
    async def fake_list(*, tenant_id):
        return []
    monkeypatch.setattr("portal.routes.repository.list_apps_for_user", fake_list)
    r = await client.get("/v1/portal/apps")
    assert r.status_code == 200
```

- [ ] **Step 9: 安装 + 运行测试**

```bash
cd services/services/portal && pip install -e . && cd -
pytest services/services/portal/tests/test_routes.py -v
```
Expected: pass。

- [ ] **Step 10: Dockerfile（镜像 admin）**

Run: `cat services/services/admin/Dockerfile` → 复制，改 SERVICE=portal / COPY services/services/portal / builder useradd（findings #1：builder 加 `useradd -m -u 1000 apihub` + `USER apihub`）/ `uvicorn portal.main:app --workers 1`（auth 同款，HA 走 replicas）。

- [ ] **Step 11: ruff/mypy + commit**

```bash
ruff check services/services/portal/ && mypy services/services/portal/
git add services/services/portal/
git commit -m "feat(portal-bff): 新服务（镜像 admin，身份转发 + app/key 自助直写）"
```

---

### Task 5: Portal 前端（镜像 frontend/admin）

**Files:**
- Create: `frontend/portal/{package.json,vite.config.ts,tsconfig.json,index.html,src/main.tsx,src/App.tsx,src/index.css,src/api/client.ts,src/store.ts,src/pages/{Register,Login,Apps}.tsx}`
- Test: `make portal-frontend-typecheck` + `portal-frontend-build`

**Interfaces:**
- Consumes: portal-bff API（`/v1/portal/auth/*` + `/v1/portal/apps*`）。
- Produces: SPA，JWT 存 `localStorage['apihub_portal_token']`。3 页。

- [ ] **Step 1: 脚手架（复制 admin 配置并改）**

```bash
mkdir -p frontend/portal/src/{api,pages}
cp frontend/admin/package.json frontend/portal/package.json   # 改 "name":"apihub-portal"
cp frontend/admin/tsconfig.json frontend/portal/tsconfig.json
cp frontend/admin/index.html frontend/portal/index.html       # 改 <title>APIHub Portal</title>
```
`frontend/portal/vite.config.ts`：
```typescript
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5174,
    proxy: {
      '/v1/portal': 'http://localhost:8011',
    },
  },
});
```

- [ ] **Step 2: `src/api/client.ts`（镜像 admin，改 JWT）**

复制 `frontend/admin/src/api/client.ts`，改：
- `API_KEY_STORAGE='apihub_portal_token'`、`USER_STORAGE='apihub_portal_user'`
- `AuthState = { token: string; user: { id: string; name: string; tenantId: string } }`
- `getAuth/setAuth/clearAuth` 用 `token`
- request 注入 `headers['Authorization'] = 'Bearer ' + auth.token`（非 X-API-Key）
- `api.post` 保留 `opts.skipAuth`（注册/登录用）

- [ ] **Step 3: `src/store.ts`**

```typescript
import { create } from 'zustand';
import { getAuth, clearAuth, AuthState } from './api/client';

interface PortalStore {
  auth: AuthState | null;
  logout: () => void;
  refresh: () => void;
}

export const useStore = create<PortalStore>((set) => ({
  auth: getAuth(),
  logout: () => { clearAuth(); set({ auth: null }); },
  refresh: () => set({ auth: getAuth() }),
}));
```

- [ ] **Step 4: `src/pages/Register.tsx`**

```tsx
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../api/client';

export function Register() {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [phone, setPhone] = useState('');
  const [name, setName] = useState('');
  const [msg, setMsg] = useState('');
  const nav = useNavigate();

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      const r = await api.post<{ verify_token: string }>(
        '/v1/portal/auth/register',
        { email, password, phone, name }, { skipAuth: true });
      await api.get(`/v1/portal/auth/verify-email?token=${r.verify_token}`);  // dev stub 自动验证
      setMsg('注册成功，跳转登录');
      setTimeout(() => nav('/login'), 800);
    } catch (e: any) {
      setMsg(e.message);
    }
  };

  return (
    <form onSubmit={submit}>
      <h2>注册</h2>
      <input value={name} onChange={(e) => setName(e.target.value)} placeholder="姓名" />
      <input value={email} onChange={(e) => setEmail(e.target.value)} placeholder="邮箱" />
      <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="密码（≥8位）" />
      <input value={phone} onChange={(e) => setPhone(e.target.value)} placeholder="手机号" />
      <button type="submit">注册</button>
      <p>{msg}</p>
    </form>
  );
}
```

- [ ] **Step 5: `src/pages/Login.tsx`**

```tsx
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, setAuth } from '../api/client';
import { useStore } from '../store';

export function Login() {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [err, setErr] = useState('');
  const nav = useNavigate();
  const refresh = useStore((s) => s.refresh);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      const r = await api.post<{ access_token: string; user: any }>(
        '/v1/portal/auth/login', { email, password }, { skipAuth: true });
      setAuth(r.access_token, r.user);
      refresh();
      nav('/apps');
    } catch (e: any) {
      setErr(e.message);
    }
  };

  return (
    <form onSubmit={submit}>
      <h2>登录</h2>
      <input value={email} onChange={(e) => setEmail(e.target.value)} placeholder="邮箱" />
      <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="密码" />
      <button type="submit">登录</button>
      <p>{err}</p>
    </form>
  );
}
```

- [ ] **Step 6: `src/pages/Apps.tsx`**

```tsx
import { useEffect, useState } from 'react';
import { api } from '../api/client';

interface App { id: string; name: string; tenant_id: string; status: string }

export function Apps() {
  const [apps, setApps] = useState<App[]>([]);
  const [name, setName] = useState('');
  const [newKey, setNewKey] = useState('');

  const load = async () => setApps(await api.get<App[]>('/v1/portal/apps'));
  useEffect(() => { load(); }, []);

  const createApp = async (e: React.FormEvent) => {
    e.preventDefault();
    await api.post('/v1/portal/apps', { name, type: 'external' });
    setName('');
    load();
  };

  const genKey = async (appId: string) => {
    const r = await api.post<{ api_key: string }>(
      `/v1/portal/apps/${appId}/api-keys`, { name: 'default' });
    setNewKey(r.api_key);
  };

  return (
    <div>
      <h2>我的应用</h2>
      <form onSubmit={createApp}>
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="应用名" />
        <button>新建应用</button>
      </form>
      <ul>
        {apps.map((a) => (
          <li key={a.id}>{a.name}（{a.tenant_id}）
            <button onClick={() => genKey(a.id)}>生成 API Key</button>
          </li>
        ))}
      </ul>
      {newKey && <p>新 Key（仅显示一次）：<code>{newKey}</code></p>}
    </div>
  );
}
```

- [ ] **Step 7: `src/App.tsx` + `src/main.tsx`**

```tsx
// App.tsx
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { Register } from './pages/Register';
import { Login } from './pages/Login';
import { Apps } from './pages/Apps';
import { useStore } from './store';

export default function App() {
  const auth = useStore((s) => s.auth);
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/register" element={<Register />} />
        <Route path="/login" element={<Login />} />
        <Route path="/apps" element={auth ? <Apps /> : <Navigate to="/login" />} />
        <Route path="*" element={<Navigate to={auth ? '/apps' : '/login'} />} />
      </Routes>
    </BrowserRouter>
  );
}
```
`src/main.tsx`：镜像 admin（`ReactDOM.createRoot(document.getElementById('root')!).render(<App />)` + `import './index.css'`）；`index.css` 从 admin 复制或最小 Tailwind import。

- [ ] **Step 8: 安装 + typecheck + build**

```bash
cd frontend/portal && npm install && cd -
make portal-frontend-typecheck
make portal-frontend-build
```
Expected: typecheck 0 error，build 产出 dist/。

- [ ] **Step 9: Commit**

```bash
git add frontend/portal/
git commit -m "feat(portal-frontend): Portal 前端（注册/登录/应用管理，JWT 鉴权）"
```

---

### Task 6: Makefile + k8s base + seed

**Files:**
- Modify: `Makefile`（.PHONY + run-portal + portal-frontend-* targets）
- Create: `deploy/k8s/base/services/portal/{deployment,service}.yaml`
- Modify: `scripts/init-db/02-seed.sql`（smoke-sync visibility=public）
- Modify: `deploy/k8s/overlays/kind/kustomization.yaml`（resources 加 portal）

- [ ] **Step 1: Makefile 加 portal 后端 target**

`run-workflow` 段（:157-159）后加：
```makefile
run-portal:  ## 本地启动 portal-bff（外部门户聚合，需要 PG + auth）
	uvicorn portal.main:app --reload --port 8011
```
.PHONY 第 3 行加 `run-portal`。

- [ ] **Step 2: Makefile 加 portal 前端 targets**

`run-admin-frontend` 段（:170-171）后加：
```makefile
# ===== Portal Frontend (Vite + React) =====
portal-frontend-install:  ## 安装 portal 前端依赖
	cd frontend/portal && npm install

portal-frontend-typecheck:  ## 仅类型检查
	cd frontend/portal && npm run typecheck

portal-frontend-build:  ## 生产构建
	cd frontend/portal && npm run build

run-portal-frontend:  ## 本地启动 portal 前端 dev server（端口 5174）
	cd frontend/portal && npm run dev -- --host
```
.PHONY 第 4 行（admin-frontend-*）加 `portal-frontend-install portal-frontend-typecheck portal-frontend-build run-portal-frontend`。

- [ ] **Step 3: k8s base deployment + service（镜像 admin）**

Run: `cat deploy/k8s/base/services/admin/deployment.yaml deploy/k8s/base/services/admin/service.yaml`
→ 写 `deploy/k8s/base/services/portal/{deployment,service}.yaml`，改：name=`portal`、image=`registry.apihub.internal/apihub/portal:0.1.0-dev`、containerPort=8011、Service port=8011、configMap 引 `portal-config`（ENV + OTEL_RESOURCE_ATTRIBUTES）、envFrom shared-infra、`securityContext.seccompProfile.type: RuntimeDefault`（findings 已加固）、startupProbe `/health/ready`（auth 同款 period 5s failureThreshold 24）。

- [ ] **Step 4: kind overlay 引 portal + smoke-sync visibility=public**

`deploy/k8s/overlays/kind/kustomization.yaml` 的 `resources:` 加 `- ../../base/services/portal`。
Run: `grep -n "smoke-sync\|smoke_sync\|visibility" scripts/init-db/02-seed.sql`
把 smoke-sync 那条 api 的 `visibility` 置为 `'public'`；若该 api 不在 seed 而是脚本内联建，在 `02-seed.sql` 末尾加幂等补丁：
```sql
UPDATE api SET visibility='public' WHERE id='smoke-sync-api';
```

- [ ] **Step 5: 验证 kustomize build**

Run:
```bash
kustomize build --load-restrictor LoadRestrictionsNone deploy/k8s/overlays/kind > /tmp/kind.yaml
grep -c "name: portal" /tmp/kind.yaml | xargs echo "portal resources (want ≥2):"
grep -c "visibility" /tmp/kind.yaml >/dev/null && echo "(visibility 不在 overlay 渲染，正常)"
```
Expected: portal Deployment + Service 出现。

- [ ] **Step 6: Commit**

```bash
git add Makefile deploy/k8s/base/services/portal/ deploy/k8s/overlays/kind/ scripts/init-db/02-seed.sql
git commit -m "feat(portal): Makefile targets + k8s base + smoke-sync visibility=public"
```

---

### Task 7: 端到端 smoke（portal-onboarding.py）

**Files:**
- Create: `scripts/smoke/portal-onboarding.py`

**Interfaces:**
- Consumes: 全链路（portal-bff → auth → PG/Redis；dispatcher visibility；APISIX）。
- Produces: smoke 断言 ①注册 ②验证 ③登录拿 JWT ④建应用 ⑤拿 Key ⑥Key 经 APISIX 调 smoke-sync(public)=200。

- [ ] **Step 1: 写 `portal-onboarding.py`（镜像 k8s-links.py 风格）**

```python
#!/usr/bin/env python3
"""外部开发者身份地基端到端 smoke。

链路：portal-bff /v1/portal/auth/* → auth（PG/Redis）→ 拿 JWT →
      portal-bff /v1/portal/apps → 拿 Key → APISIX /dispatch/smoke-sync/echo → 200。

前置：make dev-up + make run-auth + make run-portal + make run-dispatcher（或 kind 全栈）。
退出码：0 OK / 1 assert fail / 2 env unavailable。
"""

import json
import secrets
import sys
import urllib.error
import urllib.request

PORTAL_URL = "http://127.0.0.1:8011"
APISIX_URL = "http://127.0.0.1:30080"
PUBLIC_API_PATH = "/smoke-sync/echo"  # smoke-sync base_path=/smoke-sync, version path=/echo


def http(method, url, headers=None, data=None, timeout=15):
    req = urllib.request.Request(url, method=method, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def main():
    email = f"smoke_{secrets.token_hex(4)}@example.com"

    print("== ① 注册 ==")
    st, body = http("POST", f"{PORTAL_URL}/v1/portal/auth/register",
                    headers={"Content-Type": "application/json"},
                    data=json.dumps({"email": email, "password": "smoke1234",
                                     "phone": "13800000000", "name": "Smoke"}).encode())
    print(f"  register -> HTTP {st} {body[:120]!r}")
    assert st == 201, f"register HTTP {st}: {body}"
    verify_token = json.loads(body)["verify_token"]

    print("== ② 邮箱验证（dev stub token）==")
    st, body = http("GET", f"{PORTAL_URL}/v1/portal/auth/verify-email?token={verify_token}")
    print(f"  verify -> HTTP {st} {body[:120]!r}")
    assert st == 200 and json.loads(body)["status"] == "active", f"verify HTTP {st}: {body}"

    print("== ③ 登录拿 JWT ==")
    st, body = http("POST", f"{PORTAL_URL}/v1/portal/auth/login",
                    headers={"Content-Type": "application/json"},
                    data=json.dumps({"email": email, "password": "smoke1234"}).encode())
    print(f"  login -> HTTP {st}")
    assert st == 200, f"login HTTP {st}: {body}"
    token = json.loads(body)["access_token"]
    auth_hdr = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    print("== ④ 建应用 ==")
    st, body = http("POST", f"{PORTAL_URL}/v1/portal/apps", headers=auth_hdr,
                    data=json.dumps({"name": "smoke app", "type": "external"}).encode())
    print(f"  create app -> HTTP {st} {body[:120]!r}")
    assert st == 201, f"create app HTTP {st}: {body}"
    app_id = json.loads(body)["id"]

    print("== ⑤ 拿 API Key ==")
    st, body = http("POST", f"{PORTAL_URL}/v1/portal/apps/{app_id}/api-keys", headers=auth_hdr,
                    data=json.dumps({"name": "default"}).encode())
    print(f"  create key -> HTTP {st}")
    assert st == 201, f"create key HTTP {st}: {body}"
    api_key = json.loads(body)["api_key"]

    print("== ⑥ 用 Key 经 APISIX 调 smoke-sync(public) ==")
    st, body = http("GET", f"{APISIX_URL}/dispatch{PUBLIC_API_PATH}",
                    headers={"X-API-Key": api_key})
    print(f"  call public API -> HTTP {st} {body[:120]!r}")
    assert st == 200, f"call public API HTTP {st}: {body}"

    print("PORTAL-ONBOARDING OK —— 外部开发者端到端闭环（注册→验证→登录→应用→Key→调用 200）")
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"SMOKE FAIL: {e}")
        sys.exit(1)
    except (OSError, RuntimeError) as e:
        print(f"SMOKE ENV-UNAVAILABLE: {e}")
        sys.exit(2)
```

- [ ] **Step 2: 起 dev 栈 + 服务，跑 smoke**

```bash
make dev-up
make run-auth &
make run-portal &
make run-dispatcher &
sleep 5
python3 scripts/smoke/portal-onboarding.py
```
Expected: `PORTAL-ONBOARDING OK`，exit 0。⑥ HTTP 200 证明 visibility=public 对 external-public caller 放行。

- [ ] **Step 3: ruff + commit**

```bash
ruff check scripts/smoke/portal-onboarding.py
git add scripts/smoke/portal-onboarding.py
git commit -m "test(smoke): portal-onboarding 端到端（注册→...→APISIX 调通 public API 200）"
```

- [ ] **Step 4:（push/PR 等发话）**

按 push-on-ask，等用户发话再 push/squash-PR（一个 PR 覆盖 Task 1-7 + spec + 本 plan）。

---

## Self-Review（写完后核对，已 inline 修正）

- **Spec 覆盖**：4 决策 → Task1(JWT 分流)+Task2(auth 扩展)+Task3(visibility)+Task4(portal-bff)；端到端闭环 → Task7；复用表 → Global Constraints；defer 项（短信真接/enterprise/grant 表/refresh/SMTP/i18n）均未出现在任何 Task = non-goal。✅
- **Placeholder**：每步含具体代码/命令；Dockerfile/k8s/pyproject 用「cat 现有文件 + 指明改动点」（非空泛）。✅
- **类型一致**：`issue_token(user_id, tenant_id, *, is_platform_admin, secret, ttl_seconds)` Task1 定义、Task2 调用一致；`generate_api_key() -> (plaintext, hash, prefix)` Task4 调用一致；`check_visibility(snap, ctx)` Task3 定义+测试一致；`create_app(service_name, *, build_routes, skip_auth_paths)` 签名与源码一致。✅
- **redis 函数名已核对**：`apihub_core/redis.py` 导出 `t_get(key)` / `t_set(key, value, ex=)` / `t_delete(key)`（另有 t_incr/t_expire/raw_client），Task2/4 用法准确无误。
- **implementer 选型**：Task3 `resolve_by_header` 缓存分支填 visibility 的两种做法（缓存外补查 vs 纳入缓存 payload）任选其一。
