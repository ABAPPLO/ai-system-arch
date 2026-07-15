# R0a 安全合规硬底线 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the four security/compliance gaps from audit §2.4/2.5/2.6 — make the RLS-bypass session auditable, remove the SQL-injection surface in RLS session variables, forbid insecure default secrets in prod, and make the audit-archive/data-cleanup CronJobs actually run.

**Architecture:** All code changes are in `apihub-core` (shared lib) + one k8s manifest edit + one auth call-site. RLS session variables switch from f-string `SET LOCAL` to parameterized `set_config($1, $2, true)`. `admin_db_session` gains an **opt-in** `audit_reason` parameter that writes one `audit_log` row via a *separate raw connection* (avoids the recursion where admin's own `record()` writes audit through `admin_db_session`). Secret validation runs at `get_settings()` time and raises only when `env=prod` (or `REQUIRE_SECURE_SECRETS=1`).

**Tech Stack:** Python 3.11, asyncpg, pydantic-settings, structlog, pytest (`asyncio_mode=auto`), Kustomize/k8s.

## Spec refinement (decided during planning)

Spec §6.2(1) said "admin_db_session writes audit on every call." Code reality: executor/retry/quota/ai-gateway call `admin_db_session` on **every** write (hot path → perf + noise disaster), and `admin/repository.py:record()` writes audit **through** `admin_db_session` (→ infinite recursion if auto-audit). R0a therefore delivers an **opt-in** audit capability (`audit_reason` param + `audit_reason_var` contextvar, default = no audit, backward-compatible) and wires it into one genuine cross-tenant op (`auth /internal/auth/check`). Migrating executor/retry/quota **off** `admin_db_session` is deferred to R0c (service boundaries).

## Global Constraints

- Do **not** change existing call-site signatures in a breaking way — `admin_db_session()` must still work with no args.
- `env != "prod"` (and no `REQUIRE_SECURE_SECRETS=1`) **must** still accept default secrets (don't break dev/test).
- Audit writes are best-effort (swallow + log), never break the business operation.
- Integration tests need PG up (`make dev-up`); follow the `test_db_rls.py` pattern (real `asyncpg.create_pool` + `monkeypatch.setattr(db, "_pool", pool)`).
- One squash-PR at the end; commits per task are local only.

---

## Task 1: config — forbid insecure default secrets in prod

**Files:**
- Modify: `services/libs/apihub-core/src/apihub_core/config.py:9-117` (add `validate_security()` + call in `get_settings`)
- Test: `services/libs/apihub-core/tests/test_config_security.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `Settings.validate_security() -> None` (raises `RuntimeError` when insecure in prod). `get_settings()` calls it once after construction.

- [ ] **Step 1: Write the failing test**

Create `services/libs/apihub-core/tests/test_config_security.py`:

```python
"""R0a: prod 启动断言 —— 拒绝不安全默认密钥。纯单测，无 PG。"""

import pytest

from apihub_core.config import Settings


def _mk(**overrides):
    base = dict(pg_host="x", pg_user="x", pg_password="x", redis_host="x")
    base.update(overrides)
    return Settings(**base)


def test_raises_in_prod_with_default_secrets(monkeypatch):
    monkeypatch.delenv("REQUIRE_SECURE_SECRETS", raising=False)
    s = _mk(env="prod")  # 三个密钥都是默认值
    with pytest.raises(RuntimeError, match="Insecure default"):
        s.validate_security()


def test_ok_in_dev_with_default_secrets(monkeypatch):
    monkeypatch.delenv("REQUIRE_SECURE_SECRETS", raising=False)
    s = _mk(env="dev")
    s.validate_security()  # 不抛


def test_ok_in_prod_with_custom_secrets(monkeypatch):
    monkeypatch.delenv("REQUIRE_SECURE_SECRETS", raising=False)
    s = _mk(
        env="prod",
        jwt_secret="real-jwt-secret",
        pii_encryption_key="ab" * 32,  # 64 hex = 32 字节
        oss_secret_key="real-oss-secret",
    )
    s.validate_security()  # 不抛


def test_require_secure_secrets_flag_enforces_in_dev(monkeypatch):
    monkeypatch.setenv("REQUIRE_SECURE_SECRETS", "1")
    s = _mk(env="dev")  # 默认密钥 + 显式 flag
    with pytest.raises(RuntimeError, match="Insecure default"):
        s.validate_security()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/libs/apihub-core && pytest tests/test_config_security.py -v`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'validate_security'`.

- [ ] **Step 3: Write minimal implementation**

Edit `services/libs/apihub-core/src/apihub_core/config.py`. Add `import os` at top (after `from functools import lru_cache`). Replace the trailing `get_settings` block (currently lines 114-116) with:

```python
    # workflow-svc（dispatcher /v1/jobs 代理目标，文档 §4）
    # K8s 默认走集群内 DNS；dev 在 .env.dev 覆盖到 localhost
    workflow_service_url: str = "http://workflow.apihub-system"

    def validate_security(self) -> None:
        """prod（或 REQUIRE_SECURE_SECRETS=1）禁止使用不安全默认密钥。

        dev/test 仍允许默认值，便于本地启动；prod 漏配即启动失败。
        """
        enforce = self.env.lower() == "prod" or os.environ.get(
            "REQUIRE_SECURE_SECRETS"
        ) == "1"
        if not enforce:
            return
        bad = [k for k, v in _INSECURE_DEFAULTS.items() if getattr(self, k) == v]
        if bad:
            raise RuntimeError(
                f"Insecure default secrets in prod ({bad}); "
                "inject real values via env (jwt_secret/pii_encryption_key/oss_secret_key)."
            )


# 不安全默认值清单：prod 不允许使用（R0a §2.5）
_INSECURE_DEFAULTS = {
    "jwt_secret": "dev-only-insecure-secret",
    "pii_encryption_key": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
    "oss_secret_key": "apihub_dev_pwd",
}


@lru_cache
def get_settings() -> Settings:
    s = Settings()  # type: ignore[call-arg]
    s.validate_security()
    return s
```

(This places `validate_security` as a method on `Settings` and the `_INSECURE_DEFAULTS` registry + guarded `get_settings` at module level.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/libs/apihub-core && pytest tests/test_config_security.py -v`
Expected: 4 passed.

- [ ] **Step 5: Confirm no regression in existing config test**

Run: `cd services/libs/apihub-core && pytest tests/test_config.py -v`
Expected: PASS (existing tests construct with `env=dev`, so `validate_security()` is a no-op).

- [ ] **Step 6: Commit**

```bash
git add services/libs/apihub-core/src/apihub_core/config.py services/libs/apihub-core/tests/test_config_security.py
git commit -m "feat(apihub-core): forbid insecure default secrets in prod (R0a §2.5)"
```

---

## Task 2: db — parameterize RLS session variables (remove injection surface)

**Files:**
- Modify: `services/libs/apihub-core/src/apihub_core/db.py:98-113` (db_session), `:130-139` (admin_db_session), `:156-165` (meta_db_session)
- Test: `services/libs/apihub-core/tests/test_db_rls.py` (append one class)

**Interfaces:**
- Consumes: `get_tenant_context()`.
- Produces: unchanged public signatures; RLS GUCs now set via `SELECT set_config(name, $1, true)`.

- [ ] **Step 1: Write the failing test**

Append to `services/libs/apihub-core/tests/test_db_rls.py`:

```python
class TestRLSInjectionHardened:
    """R0a §2.5: db_session 用 set_config($1) 参数化，含引号的 tenant_id 不能注入/报错。"""

    async def test_db_session_handles_quote_in_tenant_id(self, monkeypatch):
        from apihub_core import db
        from apihub_core.tenant import TenantContext, set_tenant_context

        pool = await asyncpg.create_pool(PG_DSN, min_size=1, max_size=2)
        monkeypatch.setattr(db, "_pool", pool)
        # 尝试 SQL 注入：旧 f-string 实现会拼进 SQL 破坏语句或改写 RLS
        evil = TenantContext(
            tenant_id="x', 'true'); -- ",
            tenant_type="internal",
            app_id="app_trading",
        )
        try:
            set_tenant_context(evil)
            async with db.db_session() as conn:
                rows = await conn.fetch("SELECT id, tenant_id FROM api")
            # 参数化后 evil 被当字面量：RLS 过滤到该(不存在)tenant → 空，无注入、无报错
            assert rows == [], f"注入面：意外返回行 {rows}"
        finally:
            await pool.close()

    async def test_db_session_still_filters_correctly_after_param_change(self, monkeypatch, tenant_a):
        from apihub_core import db
        from apihub_core.tenant import set_tenant_context

        pool = await asyncpg.create_pool(PG_DSN, min_size=1, max_size=2)
        monkeypatch.setattr(db, "_pool", pool)
        try:
            set_tenant_context(tenant_a)
            async with db.db_session() as conn:
                rows = await conn.fetch("SELECT tenant_id FROM api")
            assert rows and all(r["tenant_id"] == "tenant_a" for r in rows)
        finally:
            await pool.close()
```

- [ ] **Step 2: Run test to verify it fails (or behaves insecurely)**

Run: `cd services/libs/apihub-core && pytest tests/test_db_rls.py::TestRLSInjectionHardened -v`
Expected: FAIL — `test_db_session_handles_quote_in_tenant_id` raises `PostgresSyntaxError` (the quote breaks the f-stringed `SET LOCAL`); the second test passes already.

- [ ] **Step 3: Write minimal implementation**

Edit `services/libs/apihub-core/src/apihub_core/db.py`. Replace the two `SET LOCAL` lines in `db_session` (currently lines 105-106):

```python
                # 注入租户上下文给 RLS 用（参数化，防 SQL 注入 —— R0a §2.5）
                await conn.execute(
                    "SELECT set_config('app.tenant_id', $1, true)", ctx.tenant_id
                )
                await conn.execute(
                    "SELECT set_config('app.is_platform_admin', $1, true)",
                    "true" if ctx.is_platform_admin else "false",
                )
                yield conn
```

Replace the `SET LOCAL` line in `admin_db_session` (currently line 134) and in `meta_db_session` (currently line 160) — both become:

```python
            await conn.execute(
                "SELECT set_config('app.is_platform_admin', $1, true)", "true"
            )
            yield conn
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/libs/apihub-core && pytest tests/test_db_rls.py::TestRLSInjectionHardened -v`
Expected: 2 passed.

- [ ] **Step 5: Confirm RLS regression suite still green**

Run: `cd services/libs/apihub-core && pytest tests/test_db_rls.py -v`
Expected: all PASS (the existing admin-bypass tests use raw connections with their own `SET LOCAL`, unaffected; `TestRLSViaDbSession` still passes).

- [ ] **Step 6: Commit**

```bash
git add services/libs/apihub-core/src/apihub_core/db.py services/libs/apihub-core/tests/test_db_rls.py
git commit -m "fix(apihub-core): parameterize RLS GUC via set_config (R0a §2.5)"
```

---

## Task 3: db — opt-in audit for admin_db_session

**Files:**
- Modify: `services/libs/apihub-core/src/apihub_core/db.py` (add `audit_reason` param + `_audit_reason_var` + `_write_admin_audit`; fix the `admin_db_session` docstring)
- Test: `services/libs/apihub-core/tests/test_db_rls.py` (append one class)

**Interfaces:**
- Consumes: `get_tenant_context()`, the `audit_log` table (`scripts/init-db/01-schema.sql:195`).
- Produces: `admin_db_session(*, audit_reason: str | None = None)`; module-level `set_audit_reason(reason) -> ContextToken` / `reset_audit_reason(token)`.

- [ ] **Step 1: Write the failing test**

Append to `services/libs/apihub-core/tests/test_db_rls.py`:

```python
class TestAdminDbSessionAudit:
    """R0a §2.4: admin_db_session 可审计（opt-in），且不递归。"""

    async def test_no_audit_by_default(self, monkeypatch):
        from apihub_core import db

        pool = await asyncpg.create_pool(PG_DSN, min_size=1, max_size=2)
        monkeypatch.setattr(db, "_pool", pool)
        try:
            async with _connect() as c:
                before = await c.fetchval(
                    "SELECT count(*) FROM audit_log WHERE action = 'admin_db_session'"
                )
            async with db.admin_db_session() as conn:
                await conn.fetchval("SELECT 1")
            async with _connect() as c:
                after = await c.fetchval(
                    "SELECT count(*) FROM audit_log WHERE action = 'admin_db_session'"
                )
            assert after == before, "默认不应写审计"
        finally:
            await pool.close()

    async def test_audits_when_reason_given(self, monkeypatch, tenant_a):
        from apihub_core import db
        from apihub_core.tenant import set_tenant_context

        pool = await asyncpg.create_pool(PG_DSN, min_size=1, max_size=2)
        monkeypatch.setattr(db, "_pool", pool)
        set_tenant_context(tenant_a)
        try:
            async with _connect() as c:
                before = await c.fetchval(
                    "SELECT count(*) FROM audit_log WHERE action = 'admin_db_session'"
                )
            async with db.admin_db_session(audit_reason="cross-tenant key verify") as conn:
                await conn.fetchval("SELECT 1")
            async with _connect() as c:
                after = await c.fetchval(
                    "SELECT count(*) FROM audit_log WHERE action = 'admin_db_session'"
                )
            assert after == before + 1, "传 audit_reason 应写一条审计"
        finally:
            await pool.close()

    async def test_audit_failure_does_not_break_operation(self, monkeypatch, tenant_a):
        """审计写失败（如 audit_log 表不存在）不能影响业务操作。"""
        from apihub_core import db
        from apihub_core.tenant import set_tenant_context

        pool = await asyncpg.create_pool(PG_DSN, min_size=1, max_size=2)
        monkeypatch.setattr(db, "_pool", pool)
        set_tenant_context(tenant_a)
        # 让 _write_admin_audit 内部 INSERT 报错：指向不存在的表
        monkeypatch.setattr(db, "_AUDIT_TABLE", "audit_log_does_not_exist")
        try:
            async with db.admin_db_session(audit_reason="x") as conn:
                val = await conn.fetchval("SELECT 1")
            assert val == 1  # 业务操作照常完成
        finally:
            await pool.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/libs/apihub-core && pytest tests/test_db_rls.py::TestAdminDbSessionAudit -v`
Expected: FAIL — `admin_db_session() got an unexpected keyword argument 'audit_reason'`.

- [ ] **Step 3: Write minimal implementation**

Edit `services/libs/apihub-core/src/apihub_core/db.py`. Add `import contextvars` at top (after `import json`). Add module-level state after `_pool: asyncpg.Pool | None = None`:

```python
_AUDIT_TABLE = "audit_log"
_audit_reason_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "audit_reason", default=None
)


def set_audit_reason(reason: str | None) -> contextvars.ContextToken[str | None]:
    """在当前协程内设默认审计 reason（HTTP 中间件用，免去逐调用传参）。"""
    return _audit_reason_var.set(reason)


def reset_audit_reason(token: contextvars.ContextToken[str | None]) -> None:
    _audit_reason_var.reset(token)
```

Add the best-effort writer (after `close_pool`):

```python
async def _write_admin_audit(reason: str) -> None:
    """用独立 raw 连接写一条审计（避免走 admin_db_session 递归）。best-effort。

    admin/repository.record() 本身走 admin_db_session 写 audit_log；若本函数也走
    admin_db_session 会无限递归。故单独 acquire 连接、单独事务、失败只 log。
    """
    import structlog

    log = structlog.get_logger("apihub_core.db")
    if _pool is None:
        return
    ctx = get_tenant_context()
    tenant_id = ctx.tenant_id if ctx else ""
    try:
        async with _pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT set_config('app.is_platform_admin', $1, true)", "true"
            )
            await conn.execute(
                f"""
                INSERT INTO {_AUDIT_TABLE}
                    (tenant_id, actor_type, action, resource_type, detail)
                VALUES ($1, 'system', 'admin_db_session', 'platform', $2::jsonb)
                """,
                tenant_id,
                json.dumps({"reason": reason}),
            )
    except Exception as e:  # best-effort：审计失败不能影响业务
        log.warning("admin_audit_write_failed", error=str(e), reason=reason)
```

Rewrite `admin_db_session` (currently lines 116-139) — new signature + audit hook + corrected docstring:

```python
@asynccontextmanager
async def admin_db_session(
    *, audit_reason: str | None = None
) -> AsyncIterator[asyncpg.Connection]:
    """超管 DB 会话 —— 绕过 RLS，可见所有租户数据。

    使用场景（仅限平台运维 + 几个特殊服务）：
      - auth 服务跨租户查 api_key（APIKey → tenant_id/app_id）
      - 平台运维跨租户排查
      - 审计聚合查询

    ⚠️ 业务代码禁用。审计是 **opt-in**：传 `audit_reason`（或经 `set_audit_reason`
    设了 contextvar）才写一条 audit_log（action='admin_db_session'）。审计用独立
    raw 连接写入、best-effort，不影响本会话事务，也不会递归（区别于
    admin/repository.record() 显式写审计的路径）。
    """
    if _pool is None:
        raise RuntimeError("DB pool not initialized. Call init_pool first.")

    reason = audit_reason or _audit_reason_var.get()
    async with _pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            await conn.execute(
                "SELECT set_config('app.is_platform_admin', $1, true)", "true"
            )
            yield conn
            await tr.commit()
        except Exception:
            await tr.rollback()
            raise
    if reason:
        await _write_admin_audit(reason)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/libs/apihub-core && pytest tests/test_db_rls.py::TestAdminDbSessionAudit -v`
Expected: 3 passed.

- [ ] **Step 5: Confirm no regression across services' admin_db_session usage**

Run: `cd services/libs/apihub-core && pytest tests/ -v && cd ../../services/admin && pytest -v`
Expected: PASS (admin's `record()` calls `admin_db_session()` with no args — still valid; no audit triggered there, no recursion).

- [ ] **Step 6: Commit**

```bash
git add services/libs/apihub-core/src/apihub_core/db.py services/libs/apihub-core/tests/test_db_rls.py
git commit -m "feat(apihub-core): opt-in audit for admin_db_session (R0a §2.4)"
```

---

## Task 4: k8s — mount admin API-key Secret in CronJobs

**Files:**
- Modify: `deploy/k8s/base/shared/audit-archive-cronjob.yaml` (add volume + volumeMount)
- Modify: `deploy/k8s/base/shared/data-cleanup-cronjob.yaml` (same)
- Create: `deploy/k8s/base/shared/admin-cron-apikey.secret.yaml` (placeholder Secret; overlays patch real value)

**Interfaces:**
- Consumes: an admin API key value (ops-provisioned; in dev = a seeded admin key).
- Produces: both CronJobs read `/etc/apihub/api-key` from a mounted Secret.

- [ ] **Step 1: Inspect the second CronJob to confirm same gap**

Run: `sed -n '15,45p' deploy/k8s/base/shared/data-cleanup-cronjob.yaml`
Expected: shows `cat /etc/apihub/api-key` with **no** `volumes:`/`volumeMounts:` (same as audit-archive).

- [ ] **Step 2: Write the manifest change**

Edit `deploy/k8s/base/shared/audit-archive-cronjob.yaml`. Under `jobTemplate.spec.template.spec` (sibling of `serviceAccountName`/`restartPolicy`), add `volumes:`, and under the `curl` container add `volumeMounts:`:

```yaml
        spec:
          serviceAccountName: admin
          restartPolicy: Never
          volumes:
            - name: admin-apikey
              secret:
                secretName: admin-cron-apikey
          containers:
            - name: curl
              image: curlimages/curl:latest
              volumeMounts:
                - name: admin-apikey
                  mountPath: /etc/apihub
                  readOnly: true
              command:
                - sh
                - -c
                - |
                  curl -sf -X POST http://admin.apihub-system/v1/admin/audit/archive \
                    -H "X-API-Key: $(cat /etc/apihub/api-key)" \
                    -H "Content-Type: application/json" \
                    -d "{}"
              resources:
                requests:
                  cpu: 50m
                  memory: 32Mi
                limits:
                  cpu: 200m
                  memory: 64Mi
```

Apply the identical `volumes:` + `volumeMounts:` additions to `deploy/k8s/base/shared/data-cleanup-cronjob.yaml`.

- [ ] **Step 3: Create the placeholder Secret manifest**

Create `deploy/k8s/base/shared/admin-cron-apikey.secret.yaml` (base placeholder; overlays MUST patch `stringData` with a real admin key):

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: admin-cron-apikey
  namespace: apihub-system
type: Opaque
stringData:
  # 占位值；各 overlay（dev/kind/staging/prod）必须 patch 成真实 admin API Key。
  # dev/kind：用 seed 的 admin app key；prod：运维通过 sealed-secrets/external-secrets 注入。
  api-key: "REPLACE_BY_OVERLAY"
```

- [ ] **Step 4: Patch the kind overlay to a working dev value**

Confirm whether an admin app key is seeded for the cron. Run: `grep -rn "admin" scripts/init-db/02-seed.sql | grep -i app`
- If a seeded admin app key exists, patch `deploy/k8s/overlays/kind/` to override `admin-cron-apikey` `api-key=<that key>`.
- If **none** exists (likely — seeds only cover tenant_a/b/ext), record this as a **follow-up**: the cron needs an "admin app + key" added to `02-seed.sql` (small, out of R0a scope — note in PR description). For now, patch the kind overlay with any valid seed key (e.g. `ak_test_a_demo001`) so the mount + transport works end-to-end in dev; a 401 from admin auth is acceptable proof that the mount fix itself works.

- [ ] **Step 5: Validate manifests build**

Run: `kubectl kustomize deploy/k8s/overlays/kind > /tmp/kind.yaml && grep -A4 "admin-cron-apikey\|volumeMounts" /tmp/kind.yaml`
Expected: rendered output contains the `admin-cron-apikey` volume + mount on both CronJobs.

- [ ] **Step 6: Commit**

```bash
git add deploy/k8s/base/shared/audit-archive-cronjob.yaml deploy/k8s/base/shared/data-cleanup-cronjob.yaml deploy/k8s/base/shared/admin-cron-apikey.secret.yaml deploy/k8s/overlays/kind/
git commit -m "fix(k8s): mount admin API-key Secret in CronJobs (R0a §2.6)"
```

---

## Task 5: wire audit into auth /internal/auth/check + regression + PR

**Files:**
- Modify: `services/services/auth/src/auth/routes.py` or `repository.py` (`/internal/auth/check` handler — pass `audit_reason`)
- Modify: `services/services/auth/src/auth/main.py` (ensure `/internal/auth/check` is in `skip_auth_paths` — audit §3.10 consistency; verify, fix if missing)
- Test: `services/services/auth/tests/test_routes.py` (add one assertion)

**Interfaces:**
- Consumes: `apihub_core.db.admin_db_session(audit_reason=...)` from Task 3.
- Produces: every cross-tenant key-verify via `/internal/auth/check` is now audited.

- [ ] **Step 1: Locate the handler and its session usage**

Run: `grep -n "internal/auth/check\|admin_db_session\|skip_auth_paths" services/services/auth/src/auth/routes.py services/services/auth/src/auth/main.py services/services/auth/src/auth/repository.py`
Expected: shows where `/internal/auth/check` opens `admin_db_session` (cross-tenant read; likely `repository.py` `get_tenant_home_region`-style) and whether the path is in `skip_auth_paths`.

- [ ] **Step 2: Write the failing test**

Append to `services/services/auth/tests/test_routes.py` (mirror existing `admin_client` fixture style). Prefer the monkeypatch form (no direct-PG dependency):

```python
async def test_internal_auth_check_audited(admin_client, monkeypatch):
    """/internal/auth/check 做 cross-tenant 读取，应触发 admin_db_session 审计。"""
    called = {}
    from apihub_core import db

    async def _spy(reason):
        called["reason"] = reason

    monkeypatch.setattr(db, "_write_admin_audit", _spy)
    resp = await admin_client.post(
        "/v1/internal/auth/check", json={"api_key": "ak_test_a_demo001"}
    )
    assert resp.status_code == 200
    assert called.get("reason"), "应传 audit_reason 触发审计"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd services/services/auth && pytest tests/test_routes.py::test_internal_auth_check_audited -v`
Expected: FAIL — `_write_admin_audit` not called (handler doesn't pass `audit_reason`).

- [ ] **Step 4: Wire audit_reason into the handler**

In whichever file Step 1 found opening `admin_db_session` for `/internal/auth/check`, change that call to:

```python
async with db.admin_db_session(audit_reason="cross-tenant api-key verify") as conn:
    ...
```

If `/internal/auth/check` is missing from `skip_auth_paths` in `main.py` (audit §3.10), add it alongside `/v1/apikey/verify`:

```python
skip_auth_paths=["/health", "/metrics", "/docs", "/openapi.json", "/v1/apikey/verify", "/v1/internal/auth/check"]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd services/services/auth && pytest tests/test_routes.py -v`
Expected: PASS (including new test).

- [ ] **Step 6: Full R0a regression**

Run:
```bash
cd services/libs/apihub-core && pytest -v
cd ../services/auth && pytest -v
cd ../admin && pytest -v
kubectl kustomize deploy/k8s/overlays/kind > /dev/null && echo "kustomize-ok"
```
Expected: all green; `kustomize-ok`.

- [ ] **Step 7: Final commit + PR description notes**

```bash
git add services/services/auth/src/auth/routes.py services/services/auth/src/auth/main.py services/services/auth/tests/test_routes.py
git commit -m "feat(auth): audit cross-tenant /internal/auth/check (R0a §2.4)"
```

PR description must note:
- R0a closes audit §2.4/2.5/2.6.
- Spec refinement: `admin_db_session` audit is **opt-in** (not every call); executor/retry/quota migration off `admin_db_session` is deferred to R0c.
- Follow-up: admin app + key seeding for the CronJob's real X-API-Key value (R0a wires the mount; value provisioning is ops).

---

## Self-Review

1. **Spec coverage**: §6.2(1)→Task 3+5; §6.2(2)→Task 2; §6.2(3)→Task 1; §6.2(4)→Task 4. All four covered. ✓
2. **Placeholder scan**: no TODO/TBD in steps. Task 4 Step 3's Secret has `api-key: "REPLACE_BY_OVERLAY"` which is a real placeholder *string* in a real manifest (intentional — overlays patch it), called out in Step 4. Task 5 Step 1 uses grep to locate the exact edit site (auth repo wasn't fully read) and Step 4 says "whichever file Step 1 found" — concrete enough; the grep target is exact. ✓
3. **Type consistency**: `admin_db_session(*, audit_reason)` signature consistent across Task 3 (defines) and Task 5 (consumes). `_write_admin_audit`, `_AUDIT_TABLE` defined in Task 3 and referenced consistently. `set_audit_reason`/`reset_audit_reason` defined but unused by later tasks (reserved for a future HTTP middleware) — not a dangling ref into undefined code. ✓
4. **Open risk**: Task 5 relies on the auth `/internal/auth/check` route existing with that exact path + payload shape (from audit §3.7). If the payload key differs, Step 2's test body adjusts — flagged in Step 1's grep. ✓
