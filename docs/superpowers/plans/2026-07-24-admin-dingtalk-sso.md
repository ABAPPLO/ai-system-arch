# Admin 控制台 SSO（钉钉 OAuth）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用钉钉 OAuth2 替换 admin 控制台的 dev-stub 登录，浏览器经真实 IdP 登录后由 auth-svc 签发带 `is_platform_admin` 的 JWT，前端切到 `Authorization: Bearer`，不再前端伪造超管身份。

**Architecture:** 复用已存在的 Bearer-JWT 鉴权通路（`apihub_core.auth.authenticate_request` 已解码 `eyJ` token 并回填 `TenantContext`）——本计划只加「钉钉 OAuth → 签 JWT」链路（auth-svc 2 端点）+ admin 前端从 `X-API-Key` 切到 `Bearer`（含 refresh），**不触动数据面鉴权**。admin 身份落 `user_account`（加 `is_platform_admin` + SSO 列），不落 `tenant_member`（admin 是平台级全局身份，跨租户操作走 `admin_db_session` 旁路 RLS，无需租户行）。

**Tech Stack:** Python 3.11 / FastAPI / asyncpg / httpx / pyjwt（均已依赖）；React 19 + TS + Vite + AntD（admin 前端已用）。无新依赖。

## Global Constraints

- **不触动数据面鉴权**：禁止改 `authenticate_request` 的 JWT/APIKey/HMAC 分流逻辑；admin 仅靠新增 Bearer token 流入既有 JWT 分支。
- **prod fail-closed 不破坏**：`Settings.validate_security()` + `_INSECURE_DEFAULTS` 既有行为不动。钉钉凭据**可选**（SSO 未启用时 admin 仍可用 X-API-Key 机器访问，spec §10）；`dingtalk_client_id` 为空时 authorize/callback 返回 503 `SSO not configured`，**不**加入 `_INSECURE_DEFAULTS`。
- **迁移幂等**：新迁移 `15-*.sql` 全部 `ADD COLUMN IF NOT EXISTS` / `CREATE ... IF NOT EXISTS`；apply 须 as owner `apihub`（同 R2b/r3q 约束），`11-*.sql` 后的迁移含事务故 apply-db 禁 `--single-transaction`（既有约束）。
- **钉钉凭据仅后端持有**：`dingtalk_client_secret` 永不回前端；prod 经 external-secrets/KMS 注入（同 HMAC key 模式）。
- **state 防 CSRF**：authorize 生成随机 state 存 Redis `t:sso:state:{state}` TTL 600s，callback 必校验+删除（一次性）。
- **命名**：FastAPI 路由 `/v1/auth/dingtalk/*`；前端 vite 代理 `/api/auth → auth-svc:8002`。
- **TDD**：每任务先写失败测试再实现；`make test`（per-service 循环）须全绿；`make lint`（ruff 0.6.x + mypy）零新错；admin `typecheck`+`build` 双绿。
- **风格**：中文注释、与既有 `identity.py`/`routes.py` 一致的 `log.info` + `ApiError(ErrorCode.*)` 模式。

## 关键使能事实（已核实）

- `apihub_core/auth.py:80` `authenticate_request` 对 `eyJ` token 调 `jwt_utils.decode_token`，payload 取 `user_id`/`tenant_id`/`is_platform_admin` 建 `TenantContext`。✅
- `jwt_utils.issue_token(*, user_id, tenant_id, secret, ttl_seconds, is_platform_admin=False)` / `issue_refresh_token(*, user_id, tenant_id, secret, ttl_seconds)` / `decode_token` / `is_jwt`（`jwt_utils.py`）。✅
- `identity.login`（`identity.py:97`）是 JWT 签发 + refresh jti 存 Redis 的模板；`refresh_access`（:138）是通用 refresh（按 refresh token 内的 `tenant_id` 签发）——admin refresh 直接复用 `/v1/auth/refresh`。✅
- auth 公开端点在 `routes.py:312+`（`/v1/auth/register` 等，"公开，skip APIKey middleware"）；`main.py:12` `skip_auth_paths` 元组是放行清单。✅
- admin 前端 `client.ts` 当前注入 `X-API-Key`；portal `client.ts` 是 Bearer+refresh 完整模板（`getRefreshToken`/`setTokens`/401→refresh→retry）。✅
- 钉钉 OAuth2 真实端点（spec §4）：authorize `https://login.dingtalk.com/oauth2/auth?client_id=..&redirect_uri=..&response_type=code&scope=openid&state=..&prompt=consent`；token `POST https://api.dingtalk.com/v1.0/oauth2/userAccessToken` `{clientId,clientSecret,grantType:"authorization_code",code}`；userinfo `GET https://api.dingtalk.com/v1.0/contact/users/me`（header `x-acs-dingtalk-access-token`）→ `unionId`/`nick`。

## 偏离 spec 说明（实现决策）

1. **不落 `tenant_member` / 不 seed `platform` 租户**（spec §6 提及）。理由：admin 是平台级全局身份，`admin_db_session` 旁路 RLS，`require_tenant()` 只读 contextvar 不查租户表；JWT 的 `tenant_id='platform'` 仅为标签。跳过可避免 FK/租户表 schema 依赖，迁移最小。若后续要 admin 出现在成员列表，单开小轮加 `platform` 租户 + 成员行。
2. **`user_account.email NOT NULL` 不改**：SSO 用户合成 email `f"{union_id}@{provider}.sso.local"`（满足 UNIQUE NOT NULL），真实身份键为新增的 `(sso_provider, sso_union_id)`。避免 nullable email 触及既有不变量。
3. **mock-mode 自验**：加 `dingtalk_mock_mode`（同 `argo_mode=stub` 哲学），kind e2e 免真实钉钉应用即可全链验证。

## File Structure

**后端（auth-svc + apihub-core）**
- Create `scripts/init-db/15-sso-user-account.sql` — 加 `user_account` 的 SSO 列 + 超管列 + 部分唯一索引。
- Modify `services/libs/apihub-core/src/apihub_core/config.py` — `Settings` 加钉钉 SSO 字段（不改 `validate_security`/`_INSECURE_DEFAULTS`）。
- Modify `services/services/auth/src/auth/identity.py` — 加 `PLATFORM_TENANT` 常量、`_bootstrap_admin_unionids()`、`upsert_sso_user()`。
- Create `services/services/auth/src/auth/dingtalk.py` — 钉钉 OAuth 客户端（build_authorize_url / exchange_token / fetch_userinfo，含 mock 分支）。
- Modify `services/services/auth/src/auth/models.py` — 加 `DingTalkCallbackRequest`。
- Modify `services/services/auth/src/auth/routes.py` — 加 `/v1/auth/dingtalk/authorize` + `/v1/auth/dingtalk/callback`。
- Modify `services/services/auth/src/auth/main.py` — `skip_auth_paths` 加两路径。
- Create `services/services/auth/tests/test_sso.py` — state/upsert/JWT/mock-mode 单测。

**前端（admin）**
- Modify `frontend/admin/src/api/client.ts` — `X-API-Key` → `Authorization: Bearer` + refresh（镜像 portal）。
- Modify `frontend/admin/src/pages/Login.tsx` — 去 dev stub，加「钉钉登录」按钮。
- Create `frontend/admin/src/pages/LoginCallback.tsx` — callback 处理。
- Modify `frontend/admin/src/App.tsx` — 加 `/login/callback` 公开路由。
- Modify `frontend/admin/vite.config.ts` — 加 `/api/auth → :8002` 代理。

**docs**
- Create `docs/admin-sso.md` — 流程/配置/安全/部署（含 prod external-secrets 注凭据 + bootstrap 超管）。

---

### Task 1: DB 迁移 + Settings 钉钉字段 + bootstrap 解析助手

**Files:**
- Create: `scripts/init-db/15-sso-user-account.sql`
- Modify: `services/libs/apihub-core/src/apihub_core/config.py`（在 `jwt_refresh_ttl_seconds`（L94）后插入 SSO 块）
- Modify: `services/services/auth/src/auth/identity.py`（加常量 + 助手）
- Test: `services/services/auth/tests/test_sso.py`（新建）

**Interfaces:**
- Produces: `Settings.dingtalk_*` / `Settings.admin_jwt_ttl_seconds` / `Settings.bootstrap_admin_dingtalk_unionids` / `Settings.dingtalk_mock_mode`；`identity.PLATFORM_TENANT = "platform"`；`identity._bootstrap_admin_unionids(settings) -> set[str]`。后续任务消费这些。

- [ ] **Step 1: 写失败测试（bootstrap 解析）**

`services/services/auth/tests/test_sso.py`:
```python
"""Admin 钉钉 SSO 单测。"""

from apihub_core.config import Settings


def test_bootstrap_unionids_parses_csv():
    s = Settings(dingtalk_client_id="x")  # 其余必填走 conftest env
    s.bootstrap_admin_dingtalk_unionids = "uid1, uid2 ,, uid3"
    from auth.identity import _bootstrap_admin_unionids

    assert _bootstrap_admin_unionids(s) == {"uid1", "uid2", "uid3"}


def test_bootstrap_unionids_empty():
    s = Settings(dingtalk_client_id="x")
    s.bootstrap_admin_dingtalk_unionids = ""
    from auth.identity import _bootstrap_admin_unionids

    assert _bootstrap_admin_unionids(s) == set()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd services/services/auth && PYTHONPATH=src python -m pytest tests/test_sso.py -v`
Expected: FAIL `ImportError: cannot import name '_bootstrap_admin_unionids'`

- [ ] **Step 3: 写迁移 SQL**

`scripts/init-db/15-sso-user-account.sql`:
```sql
-- 15-sso-user-account.sql — admin 钉钉 SSO：user_account 加 SSO 身份列 + 平台超管列。
-- 幂等：全部 IF NOT EXISTS。apply 须 as owner apihub（同 13-/14- 约束）。
-- email 保持 NOT NULL：SSO 用户由 upsert_sso_user 合成 "<union_id>@<provider>.sso.local"。

ALTER TABLE user_account ADD COLUMN IF NOT EXISTS sso_provider text;
ALTER TABLE user_account ADD COLUMN IF NOT EXISTS sso_union_id text;
ALTER TABLE user_account
    ADD COLUMN IF NOT EXISTS is_platform_admin boolean NOT NULL DEFAULT false;

-- SSO 身份唯一（仅 SSO 用户；密码用户两列 NULL，部分索引排除）。
CREATE UNIQUE INDEX IF NOT EXISTS uq_user_sso
    ON user_account (sso_provider, sso_union_id)
    WHERE sso_provider IS NOT NULL;
```

- [ ] **Step 4: 加 Settings 字段**

`services/libs/apihub-core/src/apihub_core/config.py`，在 `jwt_refresh_ttl_seconds: int = 604800` 行（L94）之后插入：
```python

    # Admin SSO（钉钉 OAuth）—— admin 控制台浏览器登录。
    # 凭据可选：dingtalk_client_id 为空 → SSO 未启用（authorize/callback 返 503），
    # admin 仍可用 X-API-Key 机器访问（spec §10）。不加入 _INSECURE_DEFAULTS。
    dingtalk_client_id: str = ""
    dingtalk_client_secret: str = ""  # noqa: S105  prod 经 external-secrets 注入
    dingtalk_corp_id: str = ""
    dingtalk_sso_redirect_uri: str = "http://localhost:5173/login/callback"
    # dev/kind：mock IdP（免真实钉钉应用即可全链 e2e），同 argo_mode=stub 哲学。
    dingtalk_mock_mode: bool = False
    # 命中即置平台超管（仅 upsert 时设，不撤）。逗号分隔 unionId。
    bootstrap_admin_dingtalk_unionids: str = ""
    admin_jwt_ttl_seconds: int = 28800  # admin access token 8h
```

- [ ] **Step 5: 加 identity 常量 + 助手**

`services/services/auth/src/auth/identity.py`，在 `EXTERNAL_PUBLIC_TENANT = "external-public"`（L17）后加：
```python

PLATFORM_TENANT = "platform"  # admin JWT tenant_id 标签（admin_db_session 旁路 RLS，无需 tenant 行）


def _bootstrap_admin_unionids(settings: "object") -> set[str]:
    """解析 BOOTSTRAP_ADMIN_DINGTALK_UNIONIDS 逗号列表（容空白/空段）。"""
    raw = getattr(settings, "bootstrap_admin_dingtalk_unionids", "") or ""
    return {part.strip() for part in raw.split(",") if part.strip()}
```

- [ ] **Step 6: 运行测试确认通过**

Run: `cd services/services/auth && PYTHONPATH=src python -m pytest tests/test_sso.py -v`
Expected: PASS（2 passed）

- [ ] **Step 7: 本地 apply 迁移（如有 dev PG；否则跳过留 kind e2e）**

Run（仅 dev 栈在跑时）: `psql -h localhost -U apihub -d apihub -f scripts/init-db/15-sso-user-account.sql`（或 `make db-apply` 若 target 存在）。
Expected: `ALTER TABLE` / `CREATE INDEX` 各一次，重跑无错（幂等）。

- [ ] **Step 8: lint**

Run: `make lint`
Expected: 0 新错（ruff 0.6.x + mypy）。

- [ ] **Step 9: 提交**

```bash
git add scripts/init-db/15-sso-user-account.sql \
        services/libs/apihub-core/src/apihub_core/config.py \
        services/services/auth/src/auth/identity.py \
        services/services/auth/tests/test_sso.py
git commit -m "feat(auth): SSO 迁移 + Settings 钉钉字段 + bootstrap 解析"
```

---

### Task 2: identity.upsert_sso_user

**Files:**
- Modify: `services/services/auth/src/auth/identity.py`（加 `upsert_sso_user`）
- Test: `services/services/auth/tests/test_sso.py`（追加）

**Interfaces:**
- Consumes: `identity._bootstrap_admin_unionids`、`identity.PLATFORM_TENANT`、`db.admin_db_session`、`apihub_core.pii.encrypt_pii`。
- Produces: `await upsert_sso_user(*, union_id: str, name: str, provider: str = "dingtalk") -> dict`，返回 `{"user_id": str, "name": str, "is_platform_admin": bool}`（首次创建或复用既有；bootstrap 命中置超管且不撤）。

- [ ] **Step 1: 写失败测试（用 fake admin_db_session）**

追加到 `tests/test_sso.py`：
```python
import pytest


class _FakeConn:
    """记录 SQL + 按预设回 fetchrow。"""

    def __init__(self, existing=None):
        self.existing = existing  # dict | None（既有的 user_account 行）
        self.executed = []

    async def fetchrow(self, sql, *args):
        if "FROM user_account WHERE sso_provider" in sql:
            return self.existing
        if "RETURNING" in sql and self.existing:
            return {"id": self.existing["id"]}
        return None

    async def execute(self, sql, *args):
        self.executed.append((sql, args))


class _FakeSession:
    def __init__(self, existing=None):
        self._conn = _FakeConn(existing)

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_upsert_sso_user_creates_new(monkeypatch):
    s = Settings(dingtalk_client_id="x")
    s.bootstrap_admin_dingtalk_unionids = "UID_ADMIN"
    monkeypatch.setattr("auth.identity.get_settings", lambda: s)
    fake = _FakeSession(existing=None)
    monkeypatch.setattr("apihub_core.db.admin_db_session", lambda **kw: fake)

    from auth import identity

    result = await identity.upsert_sso_user(union_id="UID_ADMIN", name="Alice")
    assert result["is_platform_admin"] is True
    assert result["name"] == "Alice"
    assert any("INSERT INTO user_account" in sql for sql, _ in fake._conn.executed)


@pytest.mark.asyncio
async def test_upsert_sso_user_relogin_non_admin(monkeypatch):
    s = Settings(dingtalk_client_id="x")
    s.bootstrap_admin_dingtalk_unionids = ""  # 不命中
    monkeypatch.setattr("auth.identity.get_settings", lambda: s)
    existing = {"id": "u_existing", "is_platform_admin": False}
    fake = _FakeSession(existing=existing)
    monkeypatch.setattr("apihub_core.db.admin_db_session", lambda **kw: fake)

    from auth import identity

    result = await identity.upsert_sso_user(union_id="UID_X", name="Bob")
    assert result["user_id"] == "u_existing"
    assert result["is_platform_admin"] is False
    assert not any("INSERT INTO user_account" in sql for sql, _ in fake._conn.executed)
```

- [ ] **Step 2: 运行确认失败**

Run: `cd services/services/auth && PYTHONPATH=src python -m pytest tests/test_sso.py -v`
Expected: FAIL `AttributeError: module 'auth.identity' has no attribute 'upsert_sso_user'`

- [ ] **Step 3: 实现 upsert_sso_user**

在 `identity.py`（`anonymize_user` 之前或之后均可）加：
```python
async def upsert_sso_user(*, union_id: str, name: str, provider: str = "dingtalk") -> dict:
    """SSO 登录 upsert 用户身份（admin 钉钉登录用）。

    首次登录：建 user_account（合成 email 满足 UNIQUE NOT NULL；verification_level
    'enterprise'；status 'active'）。复用：按 (provider, union_id) 命中则更新 last_login + 名字。
    bootstrap 命中 → is_platform_admin=true（仅设不撤）；未命中保留原值（默认 false）。
    不落 tenant_member（admin 是平台级全局身份，admin_db_session 旁路 RLS）。
    """
    from apihub_core.config import get_settings  # noqa: PLC0415
    from apihub_core.pii import encrypt_pii  # noqa: PLC0415

    s = get_settings()
    is_admin = union_id in _bootstrap_admin_unionids(s)
    synth_email = f"{union_id}@{provider}.sso.local"

    async with db.admin_db_session() as conn:
        row = await conn.fetchrow(
            "SELECT id, is_platform_admin FROM user_account"
            " WHERE sso_provider=$1 AND sso_union_id=$2",
            provider,
            union_id,
        )
        if row:
            user_id = row["id"]
            admin_clause = ", is_platform_admin=true" if is_admin else ""
            await conn.execute(
                "UPDATE user_account SET last_login_at=NOW(), name=$2"
                + admin_clause
                + " WHERE id=$1",
                user_id,
                encrypt_pii(name),
            )
            cur_admin = is_admin or bool(row["is_platform_admin"])
        else:
            user_id = f"u_{secrets.token_hex(8)}"
            await conn.execute(
                "INSERT INTO user_account"
                " (id, email, name, verification_level, status, sso_provider, sso_union_id,"
                "  is_platform_admin, last_login_at)"
                " VALUES ($1, $2, $3, 'enterprise', 'active', $4, $5, $6, NOW())",
                user_id,
                synth_email,
                encrypt_pii(name),
                provider,
                union_id,
                is_admin,
            )
            cur_admin = is_admin

    log.info("sso_user_upserted", user_id=user_id, provider=provider, is_platform_admin=cur_admin)
    return {"user_id": user_id, "name": name, "is_platform_admin": cur_admin}
```

- [ ] **Step 4: 运行确认通过**

Run: `cd services/services/auth && PYTHONPATH=src python -m pytest tests/test_sso.py -v`
Expected: PASS（4 passed）

- [ ] **Step 5: lint + 提交**

Run: `make lint` → 0 新错。
```bash
git add services/services/auth/src/auth/identity.py services/services/auth/tests/test_sso.py
git commit -m "feat(auth): upsert_sso_user（首次创建/复用 + bootstrap 超管）"
```

---

### Task 3: 钉钉 OAuth 客户端模块（含 mock-mode）

**Files:**
- Create: `services/services/auth/src/auth/dingtalk.py`
- Test: `services/services/auth/tests/test_sso.py`（追加）

**Interfaces:**
- Consumes: `Settings`（`dingtalk_*`、`dingtalk_mock_mode`）。
- Produces：
  - `build_authorize_url(*, client_id, redirect_uri, state) -> str`
  - `async exchange_code_for_token(*, settings, code) -> str`（返 userAccessToken；mock 模式按 code 解析）
  - `async fetch_userinfo(*, settings, access_token) -> dict`（返 `{"union_id": str, "name": str}`；mock 模式按 access_token 解析）

**mock 协议**（`dingtalk_mock_mode=true` 时）：`code` 形如 `mock:<unionId>:<name>`（e2e/本地用），token 交换回 `mock-token:<unionId>:<name>`，userinfo 据此解析。真实分支打 DingTalk OAuth2 端点。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_sso.py`：
```python
from auth import dingtalk


def test_build_authorize_url():
    url = dingtalk.build_authorize_url(
        client_id="cid",
        redirect_uri="http://localhost:5173/login/callback",
        state="xyz",
    )
    assert url.startswith("https://login.dingtalk.com/oauth2/auth?")
    assert "client_id=cid" in url
    assert "state=xyz" in url
    assert "scope=openid" in url


@pytest.mark.asyncio
async def test_mock_exchange_and_userinfo():
    s = Settings(dingtalk_client_id="cid", dingtalk_mock_mode=True)
    token = await dingtalk.exchange_code_for_token(settings=s, code="mock:UID1:Alice")
    assert token == "mock-token:UID1:Alice"
    info = await dingtalk.fetch_userinfo(settings=s, access_token=token)
    assert info == {"union_id": "UID1", "name": "Alice"}
```

- [ ] **Step 2: 运行确认失败**

Run: `cd services/services/auth && PYTHONPATH=src python -m pytest tests/test_sso.py -v`
Expected: FAIL `ModuleNotFoundError: No module named 'auth.dingtalk'`

- [ ] **Step 3: 实现 dingtalk.py**

`services/services/auth/src/auth/dingtalk.py`:
```python
"""钉钉 OAuth2 客户端（admin SSO）。

真实分支打 DingTalk OAuth2 端点；dingtalk_mock_mode=true 时走 mock 协议
（code/access_token 形如 mock:<unionId>:<name> / mock-token:<unionId>:<name>），
让 dev/kind 全链 e2e 免真实钉钉应用。
"""

from __future__ import annotations

import httpx
from apihub_core.errors import ApiError, ErrorCode

_AUTHORIZE_BASE = "https://login.dingtalk.com/oauth2/auth"
_TOKEN_URL = "https://api.dingtalk.com/v1.0/oauth2/userAccessToken"
_USERINFO_URL = "https://api.dingtalk.com/v1.0/contact/users/me"


def build_authorize_url(*, client_id: str, redirect_uri: str, state: str) -> str:
    """构造钉钉扫码授权 URL（response_type=code, scope=openid, prompt=consent）。"""
    from urllib.parse import urlencode

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid",
        "state": state,
        "prompt": "consent",
    }
    return f"{_AUTHORIZE_BASE}?{urlencode(params)}"


async def exchange_code_for_token(*, settings, code: str) -> str:
    """code → userAccessToken。mock 模式直接透传解析。"""
    if settings.dingtalk_mock_mode:
        return _mock_token_from_code(code)
    async with httpx.AsyncClient(timeout=10.0) as c:
        resp = await c.post(
            _TOKEN_URL,
            json={
                "clientId": settings.dingtalk_client_id,
                "clientSecret": settings.dingtalk_client_secret,
                "grantType": "authorization_code",
                "code": code,
            },
        )
    if resp.status_code != 200:
        raise ApiError(
            ErrorCode.UNAUTHORIZED,
            f"dingtalk token exchange failed: {resp.status_code}",
            http_status=401,
        )
    token = resp.json().get("accessToken")
    if not token:
        raise ApiError(
            ErrorCode.UNAUTHORIZED, "dingtalk token exchange: empty accessToken", http_status=401
        )
    return token


async def fetch_userinfo(*, settings, access_token: str) -> dict:
    """userAccessToken → {union_id, name}。mock 模式按 token 解析。"""
    if settings.dingtalk_mock_mode:
        return _mock_userinfo_from_token(access_token)
    async with httpx.AsyncClient(timeout=10.0) as c:
        resp = await c.get(_USERINFO_URL, headers={"x-acs-dingtalk-access-token": access_token})
    if resp.status_code != 200:
        raise ApiError(
            ErrorCode.UNAUTHORIZED, f"dingtalk userinfo failed: {resp.status_code}", http_status=401
        )
    data = resp.json()
    union_id = data.get("unionId")
    if not union_id:
        raise ApiError(
            ErrorCode.UNAUTHORIZED, "dingtalk userinfo: missing unionId", http_status=401
        )
    return {"union_id": union_id, "name": data.get("nick") or "DingTalk User"}


# ---------- mock 协议（仅 dingtalk_mock_mode=true）----------

_CODE_PREFIX = "mock:"
_TOKEN_PREFIX = "mock-token:"


def _mock_token_from_code(code: str) -> str:
    if not code.startswith(_CODE_PREFIX):
        raise ApiError(
            ErrorCode.INVALID_INPUT, "mock code must be 'mock:<unionId>:<name>'", http_status=400
        )
    return _TOKEN_PREFIX + code[len(_CODE_PREFIX):]


def _mock_userinfo_from_token(token: str) -> dict:
    if not token.startswith(_TOKEN_PREFIX):
        raise ApiError(ErrorCode.UNAUTHORIZED, "invalid mock token", http_status=401)
    payload = token[len(_TOKEN_PREFIX):]
    parts = payload.split(":", 1)
    union_id = parts[0]
    name = parts[1] if len(parts) > 1 else "Mock User"
    return {"union_id": union_id, "name": name}
```

- [ ] **Step 4: 运行确认通过**

Run: `cd services/services/auth && PYTHONPATH=src python -m pytest tests/test_sso.py -v`
Expected: PASS（6 passed）

- [ ] **Step 5: lint + 提交**

Run: `make lint` → 0 新错。
```bash
git add services/services/auth/src/auth/dingtalk.py services/services/auth/tests/test_sso.py
git commit -m "feat(auth): 钉钉 OAuth 客户端模块（含 mock-mode）"
```

---

### Task 4: authorize / callback 端点 + state Redis + skip_auth_paths

**Files:**
- Modify: `services/services/auth/src/auth/models.py`（加 `DingTalkCallbackRequest`）
- Modify: `services/services/auth/src/auth/routes.py`（加 2 端点，接在 `/v1/auth/refresh` 后）
- Modify: `services/services/auth/src/auth/main.py`（`skip_auth_paths` 加两路径）
- Test: `services/services/auth/tests/test_sso.py`（追加端点级测试）

**Interfaces:**
- Consumes: `dingtalk.*`、`identity.upsert_sso_user`、`identity.PLATFORM_TENANT`、`jwt_utils.*`、`redis.t_set/t_get/t_delete`、`Settings`。
- Produces：
  - `GET /v1/auth/dingtalk/authorize?redirect=<uri>` → `{"authorize_url": str, "state": str}`（state 存 Redis TTL 600）。
  - `POST /v1/auth/dingtalk/callback` body `{"code": str, "state": str}` → `AuthResponse`（access_token / refresh_token / expires_in / user dict）。
- 公开端点（加入 `skip_auth_paths`），靠 K8s NetworkPolicy / 反代限来源（同其它 `/v1/auth/*`）。

- [ ] **Step 1: 写失败测试（端点级，mock-mode）**

追加到 `tests/test_sso.py`：
```python
@pytest.mark.asyncio
async def test_authorize_returns_url_and_stores_state(monkeypatch, fake_redis):
    s = Settings(dingtalk_client_id="cid", dingtalk_mock_mode=True)
    monkeypatch.setattr("auth.routes.get_settings", lambda: s)
    from auth.main import app
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get(
            "/v1/auth/dingtalk/authorize", params={"redirect": "http://localhost:5173/x"}
        )
    assert r.status_code == 200
    body = r.json()
    assert body["authorize_url"].startswith("https://login.dingtalk.com/oauth2/auth?")
    st = body["state"]
    from apihub_core import redis as redis_mod

    assert await redis_mod.t_get(f"t:sso:state:{st}") == "http://localhost:5173/x"


@pytest.mark.asyncio
async def test_callback_rejects_bad_state(monkeypatch, fake_redis):
    s = Settings(dingtalk_client_id="cid", dingtalk_mock_mode=True)
    monkeypatch.setattr("auth.routes.get_settings", lambda: s)
    from auth.main import app
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post(
            "/v1/auth/dingtalk/callback",
            json={"code": "mock:UID1:Alice", "state": "never-stored"},
        )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_callback_issues_admin_jwt(monkeypatch, fake_redis):
    s = Settings(
        dingtalk_client_id="cid",
        dingtalk_mock_mode=True,
        bootstrap_admin_dingtalk_unionids="UID1",
        jwt_secret="test-secret-test-secret-test-secret",
    )
    monkeypatch.setattr("auth.routes.get_settings", lambda: s)
    monkeypatch.setattr("auth.identity.get_settings", lambda: s)
    async def _fake_upsert(*, union_id, name, provider="dingtalk"):
        return {"user_id": "u_uid1", "name": name, "is_platform_admin": True}
    monkeypatch.setattr("auth.routes.upsert_sso_user", _fake_upsert)

    from auth.main import app
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        ar = await c.get(
            "/v1/auth/dingtalk/authorize", params={"redirect": "http://localhost:5173/x"}
        )
        state = ar.json()["state"]
        r = await c.post(
            "/v1/auth/dingtalk/callback",
            json={"code": "mock:UID1:Alice", "state": state},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["user"]["is_platform_admin"] is True
    from apihub_core import jwt_utils

    payload = jwt_utils.decode_token(body["access_token"], s.jwt_secret)
    assert payload["is_platform_admin"] is True
    assert payload["tenant_id"] == "platform"
```

- [ ] **Step 2: 运行确认失败**

Run: `cd services/services/auth && PYTHONPATH=src python -m pytest tests/test_sso.py -v`
Expected: FAIL（端点不存在 → 404，断言 200 失败）

- [ ] **Step 3: 加请求模型**

`models.py`，在 `RefreshRequest` 后加：
```python
class DingTalkCallbackRequest(BaseModel):
    """钉钉 SSO 回调请求（前端 LoginCallback 提交 code+state）。"""

    code: str = Field(min_length=1)
    state: str = Field(min_length=1)
```

- [ ] **Step 4: 加 2 端点**

`routes.py`，在 `refresh_endpoint`（L340-345）之后、`delete_account` 之前加。导入补全：`from auth.identity import ..., PLATFORM_TENANT, upsert_sso_user`；`from auth.models import ..., DingTalkCallbackRequest`；`from auth.dingtalk import build_authorize_url, exchange_code_for_token, fetch_userinfo`；`from apihub_core import jwt_utils, redis`；确保 `import secrets`。

```python
    # ========== Admin 钉钉 SSO（公开，skip APIKey middleware）==========

    @app.get("/v1/auth/dingtalk/authorize")
    async def dingtalk_authorize(redirect: str):
        """生成钉钉授权 URL + state（CSRF，存 Redis TTL 600s）。

        redirect 经白名单校验（仅 admin origin），防开放重定向。SSO 未启用 → 503。
        """
        s = get_settings()
        if not s.dingtalk_client_id and not s.dingtalk_mock_mode:
            raise ApiError(ErrorCode.INTERNAL, "SSO not configured", http_status=503)
        _assert_allowed_redirect(redirect, s)
        state = secrets.token_urlsafe(24)
        await redis.t_set(f"t:sso:state:{state}", redirect, ex=600)
        url = build_authorize_url(
            client_id=s.dingtalk_client_id or "mock",
            redirect_uri=s.dingtalk_sso_redirect_uri,
            state=state,
        )
        return {"authorize_url": url, "state": state}

    @app.post("/v1/auth/dingtalk/callback", response_model=AuthResponse)
    async def dingtalk_callback(payload: DingTalkCallbackRequest):
        """code+state → 换 token → 取 unionId → upsert user → 签 admin JWT。

        state 一次性（校验后即删）。mock 模式不打真实钉钉。
        """
        s = get_settings()
        if not s.dingtalk_client_id and not s.dingtalk_mock_mode:
            raise ApiError(ErrorCode.INTERNAL, "SSO not configured", http_status=503)
        state_key = f"t:sso:state:{payload.state}"
        stored = await redis.t_get(state_key)
        if not stored:
            raise ApiError(ErrorCode.UNAUTHORIZED, "invalid or expired state", http_status=401)
        await redis.t_delete(state_key)

        access_token = await exchange_code_for_token(settings=s, code=payload.code)
        info = await fetch_userinfo(settings=s, access_token=access_token)
        user = await upsert_sso_user(
            union_id=info["union_id"], name=info["name"], provider="dingtalk"
        )

        access = jwt_utils.issue_token(
            user_id=user["user_id"],
            tenant_id=PLATFORM_TENANT,
            secret=s.jwt_secret,
            ttl_seconds=s.admin_jwt_ttl_seconds,
            is_platform_admin=user["is_platform_admin"],
        )
        refresh = jwt_utils.issue_refresh_token(
            user_id=user["user_id"],
            tenant_id=PLATFORM_TENANT,
            secret=s.jwt_secret,
            ttl_seconds=s.jwt_refresh_ttl_seconds,
        )
        rt_payload = jwt_utils.decode_token(refresh, s.jwt_secret)
        await redis.t_set(
            f"t:refresh:{rt_payload['jti']}", user["user_id"], ex=s.jwt_refresh_ttl_seconds
        )
        log.info(
            "sso_login", user_id=user["user_id"], is_platform_admin=user["is_platform_admin"]
        )
        return AuthResponse(
            access_token=access,
            refresh_token=refresh,
            expires_in=s.admin_jwt_ttl_seconds,
            user={
                "id": user["user_id"],
                "name": user["name"],
                "is_platform_admin": user["is_platform_admin"],
                "tenant_id": PLATFORM_TENANT,
            },
        )
```

并加 redirect 白名单 helper（模块级）：
```python
def _assert_allowed_redirect(redirect: str, settings) -> None:
    """仅允许 admin origin 的回跳（防开放重定向）。dev 放行 localhost。"""
    from urllib.parse import urlparse  # noqa: PLC0415

    host = (urlparse(redirect).hostname or "").lower()
    if host in ("localhost", "127.0.0.1") or host.endswith(".apihub.internal"):
        return
    allowed = (urlparse(settings.dingtalk_sso_redirect_uri).hostname or "").lower()
    if allowed and host == allowed:
        return
    raise ApiError(ErrorCode.INVALID_INPUT, "redirect origin not allowed", http_status=400)
```

- [ ] **Step 5: 加 skip_auth_paths**

`main.py`，`skip_auth_paths` 元组里 `/v1/auth/login` 后加：
```python
        "/v1/auth/dingtalk/authorize",
        "/v1/auth/dingtalk/callback",
```

- [ ] **Step 6: 运行确认通过**

Run: `cd services/services/auth && PYTHONPATH=src python -m pytest tests/test_sso.py -v`
Expected: PASS（9 passed）

- [ ] **Step 7: 全 auth 套件不回归 + lint**

Run: `cd services/services/auth && PYTHONPATH=src python -m pytest -v` → 全绿（既有不回归）。
Run: `make lint` → 0 新错。

- [ ] **Step 8: 提交**

```bash
git add services/services/auth/src/auth/models.py \
        services/services/auth/src/auth/routes.py \
        services/services/auth/src/auth/main.py \
        services/services/auth/tests/test_sso.py
git commit -m "feat(auth): 钉钉 SSO authorize/callback 端点 + state CSRF + skip_auth"
```

---

### Task 5: admin 前端 client.ts 切 Bearer + refresh

**Files:**
- Modify: `frontend/admin/src/api/client.ts`

**Interfaces:**
- Produces: `getAuth()` 返 `{token, user}`；新增 `getRefreshToken()` / `setTokens(token, refresh, user)`；`clearAuth()` 清 3 key；`request()` 注入 `Authorization: Bearer`，401→POST `/api/auth/v1/auth/refresh`→重试；`downloadCsv` 注入 Bearer。`AuthState.user` 形状 `{id, name, isPlatformAdmin, tenantId}` 不变（兼容 Layout/pages）。

- [ ] **Step 1: 重写 client.ts（镜像 portal + 保留 admin user 形状）**

把 `frontend/admin/src/api/client.ts` 整体替换为（保留 `AuthState.user` 字段名 `isPlatformAdmin`/`tenantId` 以兼容现有页面；存储 key 换为 token/refresh）：
```typescript
/**
 * Admin API client —— 统一 fetch wrapper（Bearer JWT 鉴权版）。
 *
 * 钉钉 SSO 登录后存 access/refresh JWT；401 自动 refresh 一次再重试，失败跳登录。
 * 机器访问（脚本/API Key）仍可直接带 X-API-Key 头（本 client 仅服务浏览器 SSO 态）。
 */

const TOKEN_STORAGE = 'apihub_admin_token';
const REFRESH_STORAGE = 'apihub_admin_refresh';
const USER_STORAGE = 'apihub_admin_user';

export interface AuthState {
  token: string;
  user: { id: string; name: string; isPlatformAdmin: boolean; tenantId: string };
}

export function getAuth(): AuthState | null {
  const token = localStorage.getItem(TOKEN_STORAGE);
  const userJson = localStorage.getItem(USER_STORAGE);
  if (!token || !userJson) return null;
  try {
    return { token, user: JSON.parse(userJson) };
  } catch {
    return null;
  }
}

export function getRefreshToken(): string | null {
  return localStorage.getItem(REFRESH_STORAGE);
}

export function setTokens(
  token: string,
  refreshToken: string,
  user: AuthState['user'],
): void {
  localStorage.setItem(TOKEN_STORAGE, token);
  localStorage.setItem(REFRESH_STORAGE, refreshToken);
  localStorage.setItem(USER_STORAGE, JSON.stringify(user));
}

export function clearAuth(): void {
  localStorage.removeItem(TOKEN_STORAGE);
  localStorage.removeItem(REFRESH_STORAGE);
  localStorage.removeItem(USER_STORAGE);
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: number,
    message: string,
  ) {
    super(message);
  }
}

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE';
  body?: unknown;
  skipAuth?: boolean;
  query?: Record<string, string | number | undefined | null>;
}

async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, skipAuth, query } = opts;

  let url = path;
  if (query) {
    const sp = new URLSearchParams();
    for (const [k, v] of Object.entries(query)) {
      if (v !== undefined && v !== null && v !== '') sp.set(k, String(v));
    }
    const qs = sp.toString();
    if (qs) url += (path.includes('?') ? '&' : '?') + qs;
  }

  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (!skipAuth) {
    const auth = getAuth();
    if (auth) headers['Authorization'] = 'Bearer ' + auth.token;
  }

  const resp = await fetch(url, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });

  if (resp.status === 401 && !skipAuth) {
    const rt = getRefreshToken();
    if (rt) {
      try {
        const rr = await fetch('/api/auth/v1/auth/refresh', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ refresh_token: rt }),
        });
        if (rr.ok) {
          const data = await rr.json();
          const auth = getAuth();
          setTokens(
            data.access_token,
            data.refresh_token,
            auth?.user || { id: '', name: '', isPlatformAdmin: false, tenantId: 'platform' },
          );
          headers['Authorization'] = 'Bearer ' + data.access_token;
          const retry = await fetch(url, {
            method,
            headers,
            body: body !== undefined ? JSON.stringify(body) : undefined,
          });
          if (retry.ok) {
            const ct = retry.headers.get('content-type') || '';
            return (ct.includes('application/json')
              ? await retry.json()
              : await retry.text()) as T;
          }
        }
      } catch {
        /* fall through to logout */
      }
    }
    clearAuth();
    window.location.href = '/login';
    throw new ApiError(401, 10002, 'Unauthorized');
  }

  let payload: unknown = null;
  const ct = resp.headers.get('content-type') || '';
  payload = ct.includes('application/json') ? await resp.json() : await resp.text();

  if (!resp.ok) {
    const errBody = (payload && typeof payload === 'object'
      ? (payload as { message?: string; code?: number })
      : {}) as { message?: string; code?: number };
    throw new ApiError(
      resp.status,
      errBody.code ?? resp.status,
      errBody.message || `HTTP ${resp.status}`,
    );
  }
  return payload as T;
}

export const api = {
  get: <T>(path: string, query?: RequestOptions['query']) =>
    request<T>(path, { method: 'GET', query }),
  post: <T>(path: string, body?: unknown, opts?: { skipAuth?: boolean }) =>
    request<T>(path, { method: 'POST', body, ...opts }),
  put: <T>(path: string, body?: unknown) => request<T>(path, { method: 'PUT', body }),
  del: <T>(path: string) => request<T>(path, { method: 'DELETE' }),
};

/**
 * 下载 CSV（带 Bearer）。fetch → blob → 触发浏览器下载。
 */
export async function downloadCsv(url: string, filename: string): Promise<void> {
  const headers: Record<string, string> = {};
  const auth = getAuth();
  if (auth) headers['Authorization'] = 'Bearer ' + auth.token;

  const resp = await fetch(url, { headers });
  if (!resp.ok) {
    throw new ApiError(resp.status, resp.status, `导出失败：HTTP ${resp.status}`);
  }
  const blob = await resp.blob();
  const objUrl = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = objUrl;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(objUrl);
}
```

- [ ] **Step 2: typecheck 会因 Login.tsx 旧 `setAuth` 报错（预期，T6 修）**

Run: `cd frontend/admin && npx tsc --noEmit` → 预期 Login.tsx 报 `setAuth` 不存在（T6 重写 Login）。

- [ ] **Step 3: 提交（client 切换）**

```bash
git add frontend/admin/src/api/client.ts
git commit -m "feat(admin): client.ts 切 Bearer JWT + refresh"
```

---

### Task 6: Login 钉钉按钮 + LoginCallback + 路由 + vite 代理

**Files:**
- Modify: `frontend/admin/src/pages/Login.tsx`
- Create: `frontend/admin/src/pages/LoginCallback.tsx`
- Modify: `frontend/admin/src/App.tsx`
- Modify: `frontend/admin/vite.config.ts`

**Interfaces:**
- Consumes: `client.setTokens` / `api.post`（skipAuth）。
- Produces：Login「钉钉登录」按钮（调 authorize→跳转）；LoginCallback 取 `code/state`→POST callback→`setTokens`→`navigate('/')`；`/login/callback` 公开路由；`/api/auth` 代理。

- [ ] **Step 1: 重写 Login.tsx（去 dev stub）**

整体替换 `frontend/admin/src/pages/Login.tsx`：
```tsx
import { useState } from 'react';
import { Card, Button, Typography, Alert, Space } from 'antd';

import { api } from '../api/client';

/**
 * Admin 登录 —— 钉钉 OAuth2 SSO。
 * 点「钉钉登录」→ 调 auth /v1/auth/dingtalk/authorize 拿授权 URL → 跳钉钉扫码 →
 * 回跳 /login/callback?code=..&state=.. 由 LoginCallback 换 JWT。
 * 身份（isPlatformAdmin/tenantId）由后端 JWT 签发，前端不再伪造。
 */
export default function Login() {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const start = async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.get<{ authorize_url: string }>(
        '/api/auth/v1/auth/dingtalk/authorize',
        { redirect: `${window.location.origin}/login/callback` },
      );
      window.location.href = data.authorize_url;
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'unknown error';
      setError(`发起钉钉登录失败：${msg}`);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div
      style={{
        minHeight: '100vh',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        background: '#f0f2f5',
      }}
    >
      <Card style={{ width: 400 }}>
        <Space direction="vertical" size="large" style={{ width: '100%' }}>
          <Typography.Title level={3} style={{ margin: 0 }}>
            APIHub Admin
          </Typography.Title>
          <Typography.Text type="secondary">使用钉钉账号登录管理控制台</Typography.Text>
          {error && <Alert type="error" message={error} showIcon closable />}
          <Button type="primary" loading={loading} onClick={start} block>
            钉钉登录
          </Button>
        </Space>
      </Card>
    </div>
  );
}
```

- [ ] **Step 2: 新建 LoginCallback.tsx**

`frontend/admin/src/pages/LoginCallback.tsx`：
```tsx
import { useEffect, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { Spin, Alert } from 'antd';

import { api, setTokens } from '../api/client';

interface SsoResponse {
  access_token: string;
  refresh_token: string;
  user: { id: string; name: string; is_platform_admin: boolean; tenant_id: string };
}

/** 钉钉回跳处理：code+state → 换 JWT → 进首页。 */
export default function LoginCallback() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const code = params.get('code');
    const state = params.get('state');
    if (!code || !state) {
      setError('缺少 code/state 参数');
      return;
    }
    api
      .post<SsoResponse>(
        '/api/auth/v1/auth/dingtalk/callback',
        { code, state },
        { skipAuth: true },
      )
      .then((data) => {
        setTokens(data.access_token, data.refresh_token, {
          id: data.user.id,
          name: data.user.name,
          isPlatformAdmin: data.user.is_platform_admin,
          tenantId: data.user.tenant_id,
        });
        navigate('/');
      })
      .catch((e: unknown) => {
        setError(e instanceof Error ? e.message : '登录失败');
      });
  }, [params, navigate]);

  if (error) {
    return (
      <div style={{ padding: 64, textAlign: 'center' }}>
        <Alert type="error" message={`登录回调失败：${error}`} showIcon />
      </div>
    );
  }
  return (
    <div style={{ padding: 64, textAlign: 'center' }}>
      <Spin size="large" />
    </div>
  );
}
```

- [ ] **Step 3: 加公开路由**

`App.tsx`：在 `import Login from './pages/Login';` 下加 `import LoginCallback from './pages/LoginCallback';`，并在 `<Route path="/login" element={<Login />} />` 后加：
```tsx
      <Route path="/login/callback" element={<LoginCallback />} />
```

- [ ] **Step 4: 加 vite 代理**

`vite.config.ts`：`targets` 加 `auth: 'http://localhost:8002',`；`proxy` 块加（与其它同级）：
```typescript
      '/api/auth': {
        target: targets.auth,
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api\/auth/, ''),
      },
```

- [ ] **Step 5: typecheck + build**

Run: `cd frontend/admin && npx tsc --noEmit` → 0 error（grep 残留 `apiKey`/`setAuth`，逐处对齐 `{token}`/`setTokens`）。
Run: `cd frontend/admin && npm run build` → 构建成功。

- [ ] **Step 6: 提交**

```bash
git add frontend/admin/src/pages/Login.tsx \
        frontend/admin/src/pages/LoginCallback.tsx \
        frontend/admin/src/App.tsx \
        frontend/admin/vite.config.ts
git commit -m "feat(admin): 钉钉登录按钮 + LoginCallback + 路由 + /api/auth 代理"
```

---

### Task 7: kind 全链 e2e（mock-mode）+ 文档

**Files:**
- Create: `docs/admin-sso.md`
- Modify: auth kind env（注入 `DINGTALK_MOCK_MODE` 等，**仅 kind**；prod 留空走真实）—— 按 auth configmap/overlay 既有惯例；apply 迁移 15（幂等回放 init-db as owner `apihub`）。

**目标**：在 `kind-apihub` 集群验全链：authorize→callback（mock code）→拿 JWT→带 Bearer 调 admin 端点→200；重放 state→401；非超管 unionId→`is_platform_admin=false`。

- [ ] **Step 1: 写文档 `docs/admin-sso.md`**

内容含：登录流程图（同 spec §3）、后端端点契约、env 清单（`DINGTALK_CLIENT_ID/SECRET/CORP_ID`、`DINGTALK_SSO_REDIRECT_URI`、`DINGTALK_MOCK_MODE`、`BOOTSTRAP_ADMIN_DINGTALK_UNIONIDS`、`ADMIN_JWT_TTL_SECONDS`）、安全（state CSRF / client_secret 仅后端 / redirect 白名单 / JWT 不可前端伪造）、部署（prod 经 external-secrets 注凭据 + bootstrap 超管 + apply 迁移 15 + kind mock-mode e2e 步骤）、范围外（portal 不动 / 仅 is_platform_admin 二值 / API Key 机器访问并存）。

- [ ] **Step 2: kind 注入 mock-mode env**

确认 `kind-apihub` context 在线（`kubectl config current-context`）。在 auth 的 kind env 设：
```
DINGTALK_MOCK_MODE=true
BOOTSTRAP_ADMIN_DINGTALK_UNIONIDS=UID_ADMIN
JWT_SECRET=<与集群既有 dev jwt secret 一致>
```
（按既有 auth configmap/`.env.dev` + kind envFrom 惯例；`scripts/k8s/apply.sh kind` 或 `make k8s-apply-dev` 重 apply。）

- [ ] **Step 3: apply 迁移 15 + bounce auth**

Run（kind 内 PG，as owner `apihub`）:
```bash
kubectl -n apihub-system exec -i deploy/postgres -- \
  psql -U apihub -d apihub < scripts/init-db/15-sso-user-account.sql
kubectl -n apihub-system rollout restart deploy/auth
```
等 `/health/ready` 200。

- [ ] **Step 4: 全链 e2e 脚本（port-forward auth :8002 + admin :8006）**

```bash
kubectl -n apihub-system port-forward deploy/auth 8002:8002 &
kubectl -n apihub-system port-forward deploy/admin 8006:8006 &
# 1. authorize 拿 state
STATE=$(curl -s "http://localhost:8002/v1/auth/dingtalk/authorize?redirect=http://localhost:5173/login/callback" | jq -r .state)
# 2. callback（mock code = mock:<unionId>:<name>）
RESP=$(curl -s -X POST http://localhost:8002/v1/auth/dingtalk/callback \
  -H 'Content-Type: application/json' \
  -d "{\"code\":\"mock:UID_ADMIN:Admin\",\"state\":\"$STATE\"}")
JWT=$(echo "$RESP" | jq -r .access_token)
echo "$RESP" | jq .user                   # 期望 is_platform_admin=true
# 3. 用 JWT 调 admin（经 authenticate_request JWT 分流）
curl -s -H "Authorization: Bearer $JWT" http://localhost:8006/v1/admin/dashboard | jq .   # 期望 200
# 4. 重放同一 state（已消费）→ 401
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8002/v1/auth/dingtalk/callback \
  -H 'Content-Type: application/json' -d "{\"code\":\"mock:UID_ADMIN:Admin\",\"state\":\"$STATE\"}"   # 期望 401
# 5. 非超管 unionId → is_platform_admin=false
S2=$(curl -s "http://localhost:8002/v1/auth/dingtalk/authorize?redirect=http://localhost:5173/login/callback" | jq -r .state)
curl -s -X POST http://localhost:8002/v1/auth/dingtalk/callback \
  -H 'Content-Type: application/json' -d "{\"code\":\"mock:UID_OTHER:Other\",\"state\":\"$S2\"}" | jq .user.is_platform_admin   # 期望 false
```
Expected: 步骤 3 返 200 + dashboard JSON；步骤 4 返 401；步骤 5 返 `false`。

- [ ] **Step 5: lint + 全套件 + 提交**

Run: `make lint` → 0 新错。Run: `make test` → 全绿。
```bash
git add docs/admin-sso.md deploy/k8s/ scripts/
git commit -m "docs+chore: admin 钉钉 SSO 文档 + kind mock-mode 全链 e2e"
```

---

## Self-Review

**1. Spec 覆盖**（对照 spec §4-§11）：
- §4 后端 2 端点 + state Redis → Task 4 ✓
- §4 upsert + 超管判定 → Task 2 + bootstrap（Task 1）✓
- §5 前端 Login/LoginCallback/client Bearer/refresh/路由/代理 → Task 5+6 ✓
- §6 is_platform_admin 列 + bootstrap → Task 1（列）+ Task 2（逻辑）✓（偏离：不落 tenant_member，已说明）
- §7 安全（state CSRF / client_secret 仅后端 / redirect 白名单 / JWT 不可伪）→ Task 4（state+白名单）+ Task 1（secret 仅 Settings）+ 全局（Bearer 走既有 JWT 验签）✓
- §8 env 清单 → Task 1 Settings ✓；prod external-secrets → Task 7 文档 ✓
- §9 待用户提供 → 全 env-wired（REPLACE_ME 由 ops 注），Task 7 文档列部署前置 ✓
- §11 T1-T7 → 本计划 Task 1-7 一一对应 ✓

**2. 占位符扫描**：无 TBD/TODO；每步含实代码/命令/期望。Settings 字段、迁移 SQL、upsert、dingtalk 客户端、端点、前端均为完整代码。

**3. 类型/命名一致性**：
- `issue_token(user_id, tenant_id, secret, ttl_seconds, is_platform_admin)` 全链一致（Task 2/4）✓
- `upsert_sso_user(*, union_id, name, provider="dingtalk") -> {user_id, name, is_platform_admin}` 调用方（Task 4）与定义（Task 2）一致 ✓
- `exchange_code_for_token(*, settings, code)` / `fetch_userinfo(*, settings, access_token)` 签名一致 ✓
- 前端 `setTokens(token, refresh, user)` / `getAuth().token` / `getRefreshToken()` 一致（Task 5/6）✓
- `Settings` 字段名（`dingtalk_mock_mode`/`bootstrap_admin_dingtalk_unionids`/`admin_jwt_ttl_seconds`）跨任务一致 ✓

**风险/defer（实现时留意，非阻断）**：
- admin refresh 经 `/v1/auth/refresh` 会按 `jwt_ttl_seconds`(2h) 签发（非 admin 8h）——可接受；若要 8h 须在 refresh_access 按 tenant_id='platform' 分流，留 follow-up。
- 真实钉钉端点字段名（`accessToken`/`unionId`/`nick`）以 Task 7 真实 corp 测试为准（mock 路径已覆盖逻辑）；首次接真实应用时按实际响应微调。
- prod：`dingtalk_sso_redirect_uri` 须与钉钉应用配置的回跳一致；`BOOTSTRAP_ADMIN_DINGTALK_UNIONIDS` 至少含一个超管，否则登录后无人可管（只读视角）。

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-07-24-admin-dingtalk-sso.md`. Two execution options:**

**1. Subagent-Driven（推荐）** — 按 superpowers:subagent-driven-development，每 Task 派新 subagent，任务间 review（契合本仓 spec→plan→handoff + one-squash-PR-per-round 习惯）。

**2. Inline Execution** — 本会话按 superpowers:executing-plans 批量执行 + checkpoint review。

**Which approach?**
