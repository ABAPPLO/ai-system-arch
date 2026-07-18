# R2e — GDPR erasure 合规/正确性收尾 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修 R2d 终审浮出的 3 条 erasure 遗留——erasure 写审计、不再吊销租户级 api_key、export 砍掉租户级共享数据块。

**Architecture:** 全部改动集中在 `services/services/auth/src/auth/identity.py`（`anonymize_user` + `export_user_data`）。依据：`app`/`api_key` 表无 user 归属字段、external-public 是共享租户 → 租户级 key/billing 非个人数据，erasure/portability 不该动。TDD，每 task 独立 commit。

**Tech Stack:** Python 3.11, asyncpg, pytest（`asyncio_mode=auto`）, apihub_core（`db.admin_db_session` 的 `audit_reason`）, 真 PG + fakeredis 测试栈。

**Spec:** `docs/superpowers/specs/2026-07-18-r2e-erasure-followup-design.md`

## Global Constraints

- **python/pytest 走 repo-root `.venv/bin/python`**（NOT `services/services/auth/.venv`——那是 py3.14 stub、无 pytest）。命令前缀：`.venv/bin/python -m pytest ...`
- **测试需 dev 栈 PG**（`make dev-up`；PG 暴露 host `:15433`，user `apihub`/`apihub_dev_pwd`，db `apihub`）。无 PG 则 `test_identity.py` 整模块 skip。
- **GateGuard**：每文件首次 bash/edit 会被拦，要求报 facts 后 retry（或 `ECC_GATEGUARD=off`）。
- **`EXTERNAL_PUBLIC_TENANT` 常量保留**：`verify_email`（identity.py:76,80）/`login`（:98,102,109）仍用；仅 anonymize_user（#2 删 scrub 后）与 export_user_data（#3 砍后）不再用它。删后不会触发 ruff F401。
- **audit_log 写入 best-effort**：`_write_admin_audit`（db.py:127）用独立 raw 连接、事务 commit 之后写、失败只 log，不阻塞 erasure、不回滚、不递归。
- **每 task 一个 commit**；TDD（先写失败测试跑红 → 实现 → 跑绿 → commit）。
- **不动**：portal、apihub_core/db.py（#1 用现成 `audit_reason` 参数）、其他服务。
- **行号是改前基准**；docstring 改动会偏移后续行号，删代码按**代码块内容匹配**而非行号。

---

### Task 1: erasure 写审计（anonymize_user 加 audit_reason）

**Files:**
- Modify: `services/services/auth/src/auth/identity.py`（anonymize_user 内 `db.admin_db_session()` 调用，约 L152）
- Test: `services/services/auth/tests/test_identity.py`（新增 `test_anonymize_writes_audit_log`）

**Interfaces:**
- Consumes: `db.admin_db_session(*, audit_reason: str | None = None)`（apihub_core/db.py:198，已存在，无需改 db.py）
- Produces: anonymize_user 每次调用落一条 audit_log（`action='admin_db_session'`, `detail={"reason":"gdpr_erasure"}`）；withdraw_consent / delete_account 等所有调用路径自动受益。

audit_log 行（`_write_admin_audit`, db.py:145-153）：
```sql
INSERT INTO audit_log (tenant_id, actor_type, action, resource_type, detail)
VALUES ($tenant_id, 'system', 'admin_db_session', 'platform', '{"reason":"gdpr_erasure"}'::jsonb)
```

- [ ] **Step 1: 写失败测试**

在 `test_identity.py` 末尾追加：
```python
@pytest.mark.asyncio
async def test_anonymize_writes_audit_log(fake_redis):
    """anonymize 落一条 audit_log（reason=gdpr_erasure）。"""
    from apihub_core import db as db_mod

    user = await identity.create_user(
        email="new@example.com", password="secret123", phone="138", name="Audit"
    )
    uid = user["user_id"]

    async with db_mod.admin_db_session() as conn:
        before = await conn.fetchval(
            "SELECT COUNT(*) FROM audit_log"
            " WHERE action='admin_db_session' AND detail->>'reason' = 'gdpr_erasure'"
        )

    await identity.anonymize_user(user_id=uid)

    async with db_mod.admin_db_session() as conn:
        after = await conn.fetchval(
            "SELECT COUNT(*) FROM audit_log"
            " WHERE action='admin_db_session' AND detail->>'reason' = 'gdpr_erasure'"
        )
    assert after == before + 1
```

- [ ] **Step 2: 跑红**

```bash
.venv/bin/python -m pytest services/services/auth/tests/test_identity.py::test_anonymize_writes_audit_log -v
```
Expected: FAIL（`assert 0 == 1`——当前 anonymize 不传 audit_reason，不写 audit）。

- [ ] **Step 3: 实现——加 audit_reason**

identity.py anonymize_user 内（约 L152）：
```python
# 前
    async with db.admin_db_session() as conn:
# 后
    async with db.admin_db_session(audit_reason="gdpr_erasure") as conn:
```

- [ ] **Step 4: 跑绿**

```bash
.venv/bin/python -m pytest services/services/auth/tests/test_identity.py::test_anonymize_writes_audit_log -v
.venv/bin/python -m pytest services/services/auth/tests/test_identity.py -v
```
Expected: 新测试 PASS；全文件不回归。

- [ ] **Step 5: lint + commit**

```bash
ruff check services/services/auth/src/auth/identity.py services/services/auth/tests/test_identity.py
git add services/services/auth/src/auth/identity.py services/services/auth/tests/test_identity.py
git commit -m "feat(r2e): anonymize_user 传 audit_reason=gdpr_erasure，erasure 落 audit_log"
```
Expected: ruff 0 new。

---

### Task 2: 删 api_key scrub（erasure 不吊销租户级 key）

**Files:**
- Modify: `services/services/auth/src/auth/identity.py`（anonymize_user docstring 约 L145-151 + 删 api_key scrub 段约 L174-182）
- Test: `services/services/auth/tests/test_identity.py`（新增 `test_anonymize_does_not_revoke_api_keys`）

**Interfaces:**
- Produces: anonymize_user 不再动 `api_key`/`app` 表。操作链 = user_account 匿名化 → tenant_member 删 → user_consent 删 → notification_log scrub → Redis 清理（→ audit_log）。

app 必填列（01-schema.sql:62）：`id/tenant_id/name`（type 默认 internal，status 默认 active）。
api_key 必填列（:76）：`id/tenant_id/app_id/key_prefix/key_hash/name`（scopes 默认 `'{}'`，status 默认 active）。

- [ ] **Step 1: 写失败测试**

在 `test_identity.py` 末尾追加：
```python
@pytest.mark.asyncio
async def test_anonymize_does_not_revoke_api_keys(fake_redis):
    """erasure 不吊销租户级 api_key（external-public 共享、无 user 归属）。"""
    from apihub_core import db as db_mod

    user = await identity.create_user(
        email="new@example.com", password="secret123", phone="138", name="Key"
    )
    uid = user["user_id"]
    app_id, key_id = "app_test_r2e", "key_test_r2e"

    try:
        async with db_mod.admin_db_session() as conn:
            await conn.execute(
                "INSERT INTO app (id, tenant_id, name, type, status)"
                " VALUES ($1, $2, 'R2e Test App', 'server', 'active')"
                " ON CONFLICT (id) DO NOTHING",
                app_id, identity.EXTERNAL_PUBLIC_TENANT,
            )
            await conn.execute(
                "INSERT INTO api_key (id, tenant_id, app_id, key_prefix, key_hash,"
                " name, scopes, status)"
                " VALUES ($1, $2, $3, 'sk_test__', 'hash_test_r2e', 'R2e Key', '{}', 'active')"
                " ON CONFLICT (id) DO NOTHING",
                key_id, identity.EXTERNAL_PUBLIC_TENANT, app_id,
            )

        await identity.anonymize_user(user_id=uid)

        async with db_mod.admin_db_session() as conn:
            status = await conn.fetchval(
                "SELECT status FROM api_key WHERE id = $1", key_id
            )
        assert status == "active"
    finally:
        async with db_mod.admin_db_session() as conn:
            await conn.execute("DELETE FROM api_key WHERE id = $1", key_id)
            await conn.execute("DELETE FROM app WHERE id = $1", app_id)
```

- [ ] **Step 2: 跑红**

```bash
.venv/bin/python -m pytest services/services/auth/tests/test_identity.py::test_anonymize_does_not_revoke_api_keys -v
```
Expected: FAIL（`assert 'revoked' == 'active'`——当前 anonymize 吊销 external-public 所有 app 的 active key）。

- [ ] **Step 3: 实现——删 scrub 段 + 改 docstring**

docstring（anonymize_user 起，约 L145-151）整体替换为：
```python
    """匿名化用户账号（GDPR Right to erasure）。

    匿名化而非物理删除，保护外键完整性。
    操作：user_account 匿名化 → tenant_member 删除 → user_consent 删除 →
    notification_log 投递日志清理（按旧 recipient=email）→ Redis 清理。
    租户级 api_key 不动（external-public 共享租户，app/api_key 无 user 归属字段，
    erasure 仅清该用户本人 PII）。全程在同一 admin_db_session 事务内
    （任一失败回滚，不残留半擦除状态），并写一条 audit_log(reason=gdpr_erasure)。
    """
```

删 scrub 段（位于 notification_log 的 `if old_email:` 块之后、Redis `try:` 清理之前）——按内容匹配删除整段：
```python
        apps = await conn.fetch(
            "SELECT id FROM app WHERE tenant_id = $1", EXTERNAL_PUBLIC_TENANT,
        )
        for app in apps:
            await conn.execute(
                "UPDATE api_key SET status='revoked', revoked_at=NOW()"
                " WHERE app_id=$1 AND status='active'",
                app["id"],
            )
```

- [ ] **Step 4: 跑绿**

```bash
.venv/bin/python -m pytest services/services/auth/tests/test_identity.py::test_anonymize_does_not_revoke_api_keys -v
.venv/bin/python -m pytest services/services/auth/tests/test_identity.py -v
```
Expected: 新测试 PASS；全文件不回归。

- [ ] **Step 5: lint + commit**

```bash
ruff check services/services/auth/src/auth/identity.py services/services/auth/tests/test_identity.py
git add services/services/auth/src/auth/identity.py services/services/auth/tests/test_identity.py
git commit -m "fix(r2e): 删 anonymize 的 api_key scrub——租户级共享 key 不属个人数据，erasure 不吊销"
```
Expected: ruff 0 new。

---

### Task 3: export 砍租户级三块

**Files:**
- Modify: `services/services/auth/src/auth/identity.py`（`export_user_data` 约 L234-309，删 apps/api_keys/billing_records 查询与 return 字段）
- Test: `services/services/auth/tests/test_identity.py`（新增 `test_export_excludes_tenant_scoped_data`；验证原 2 红测试转绿）

**Interfaces:**
- Produces: `export_user_data` 返回 `{user_id, exported_at, account, tenants}`（无 apps/api_keys/billing_records）。

- [ ] **Step 1: 写失败测试**

在 `test_identity.py` 末尾追加：
```python
@pytest.mark.asyncio
async def test_export_excludes_tenant_scoped_data(fake_redis):
    """export 只含个人数据，不含租户级 apps/api_keys/billing_records。"""
    user = await identity.create_user(
        email="new@example.com", password="secret123", phone="138", name="Excl"
    )
    uid = user["user_id"]
    await identity.verify_email(user["verify_token"])

    data = await identity.export_user_data(user_id=uid)

    assert "account" in data
    assert "tenants" in data
    assert "apps" not in data
    assert "api_keys" not in data
    assert "billing_records" not in data
```

- [ ] **Step 2: 跑红**

```bash
.venv/bin/python -m pytest services/services/auth/tests/test_identity.py::test_export_excludes_tenant_scoped_data -v
```
Expected: FAIL（当前 export 查 billing_record 用不存在的列 `period/plan_name/total_calls/total_tokens/base_cents/overage_cents` → asyncpg `UndefinedColumnError`，`await export_user_data` 抛错）。

- [ ] **Step 3: 实现——`export_user_data` 整体替换为**

```python
async def export_user_data(*, user_id: str) -> dict:
    """导出用户个人数据（GDPR Right to portability）。

    含：账户信息、租户关系。租户级共享数据（apps/api_keys/billing_records）
    不归属个人（external-public 共享租户、无 user 归属字段），不在导出范围。
    """
    async with db.admin_db_session() as conn:
        row = await conn.fetchrow(
            "SELECT id, email, phone, name, verification_level, status,"
            " created_at FROM user_account WHERE id = $1", user_id,
        )
        if not row:
            raise ApiError(ErrorCode.NOT_FOUND, "user not found", http_status=404)

        members = await conn.fetch(
            "SELECT tenant_id, role FROM tenant_member WHERE user_id = $1", user_id,
        )

    from apihub_core.pii import maybe_decrypt  # noqa: PLC0415

    return {
        "user_id": row["id"],
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "account": {
            "email": row["email"],
            "phone": maybe_decrypt(row["phone"] or ""),
            "name": maybe_decrypt(row["name"] or ""),
            "verification_level": row["verification_level"],
            "status": row["status"],
            "created_at": row["created_at"].isoformat(),
        },
        "tenants": [{"tenant_id": m["tenant_id"], "role": m["role"]} for m in members],
    }
```

- [ ] **Step 4: 跑绿（含原 2 红测试转绿）**

```bash
.venv/bin/python -m pytest services/services/auth/tests/test_identity.py::test_export_excludes_tenant_scoped_data -v
.venv/bin/python -m pytest services/services/auth/tests/test_identity.py::test_export_user_data_returns_account services/services/auth/tests/test_identity.py::test_export_user_data_includes_tenants -v
.venv/bin/python -m pytest services/services/auth/tests/test_identity.py -v
```
Expected: 新测试 PASS；`returns_account`/`includes_tenants` 由 RED→GREEN（不再触发 billing 查询）；全文件不回归。

- [ ] **Step 5: lint + commit**

```bash
ruff check services/services/auth/src/auth/identity.py services/services/auth/tests/test_identity.py
mypy services/services/auth/src/auth/identity.py
git add services/services/auth/src/auth/identity.py services/services/auth/tests/test_identity.py
git commit -m "fix(r2e): export_user_data 砍租户级三块（apps/api_keys/billing_records）+ 修 billing 列名 500"
```
Expected: ruff 0 new；mypy clean。

---

### Task 4: kind e2e 验证（erasure 链 + audit + key 不被动）

**Files:** 无源码改动（纯验证；若发现打包/部署 bug 则修 + commit）。

cluster: `kind-apihub`。复用 R2d T3 流程（见 `.superpowers/sdd/progress.md` Task 3）。

- [ ] **Step 1: 确认 dev 栈 + 集群可用**

```bash
kubectl config current-context                          # 期望 kind-apihub
kubectl get pod -n apihub-system -l app=auth            # 期望 1/1 Running
```
若集群未起：`make dev-up` + 起 kind 集群（参考 R2d T3）。

- [ ] **Step 2: 重建 auth 镜像 + 加载 + rollout**

```bash
docker build -t auth:0.1.0-dev -f services/services/auth/Dockerfile services/services/auth
kind load docker-image auth:0.1.0-dev --name kind-apihub
kubectl rollout restart deploy/auth -n apihub-system
kubectl rollout status deploy/auth -n apihub-system     # 期望 successfully rolled out
```
（注：R2d T3 发现 apihub-core 已声明 cryptography；若 auth 镜像启动 `/health/ready` 200 即说明依赖到位。）

- [ ] **Step 3: 跑 erasure 链 + 断言**

经 portal/auth 走 register → verify → login → consent/withdraw（复用 R2d T3 步骤）。withdraw 后断言：
- `user_account.status='deleted'` 且 `email LIKE '%@anonymized'`
- `notification_log` 中该 recipient 行 = 0（R2d 已验，回归）
- **新增**：`audit_log` 存在 `action='admin_db_session' AND detail->>'reason'='gdpr_erasure'`
- **新增**：预置的 external-public active api_key 仍 `status='active'`（回归 #2）

查 audit_log（pod 名按 `kubectl get pod -n apihub-system | grep pg` 实际替换）：
```bash
kubectl exec -n apihub-system apihub-pg-0 -- psql -U apihub -d apihub -c \
  "SELECT action, detail->>'reason' AS reason FROM audit_log WHERE detail->>'reason'='gdpr_erasure' ORDER BY id DESC LIMIT 5;"
```

- [ ] **Step 4: 记录 / 修 bug**

无源码改动则跳过 commit；在 `.superpowers/sdd/progress.md` 记 e2e PASS（含 3 条断言）。若发现 bug（如打包漏依赖）则修 + commit，同 R2d T3 `c2f8f4c` 模式。

---
