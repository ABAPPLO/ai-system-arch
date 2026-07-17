# R2d GDPR erasure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development (or executing-plans). Steps use `- [ ]` checkboxes.

**Goal:** 闭合 GDPR erasure 链路：`withdraw_consent` 委托 `anonymize_user` + 扩 anonymize scrub `notification_log`(recipient=email) + 修 portal 4 handler 缺 `/v1` 的 M4 bug + URL 断言测试。

**Architecture:** `auth/identity.py` 是 erasure 权威：`anonymize_user` 经 `admin_db_session` 跨聚合 scrub（user_account/tenant_member/user_consent/api_key/Redis + 新增 notification_log by recipient）。`withdraw_consent` 改为委托它（兑现 docstring）。portal 4 handler 复用既有 `_forward(method,"/v1/...",headers)` 替代手拼 URL。

**Tech Stack:** Python 3.11 / FastAPI / asyncpg（admin_db_session）/ httpx / pytest（auth 用真 dev PG + fake_redis；portal 用 httpx mock）。

## Global Constraints

- **`withdraw_consent` 委托 `anonymize_user`**：不再单独 UPDATE user_consent（anonymize 已 DELETE）；保留 `log.info`。
- **`anonymize_user` 扩 notification_log**：`SELECT id,email`（取 old_email）；`DELETE FROM notification_log WHERE recipient=$1`（`if old_email:` 守卫），同 admin_db_session 事务内，在 `DELETE FROM user_consent` 之后、Redis 清理之前。
- **聚合所有权**：auth 跨聚合删 notification_log 经 admin_db_session 可接受（erasure 权威，§9-B 针对 BFF；spec 已记录权衡）。
- **portal 4 handler** 走 `_forward(method, "/v1/auth/...", headers={"Authorization": ...})`，`st>=400 → ApiError(ErrorCode.INTERNAL, body, http_status=st)`，删内联 httpx/手拼 URL/手写 error parse。
- **auth 测试是真 dev PG**（`db.admin_db_session`，仅 redis fake）——需 dev PG 在跑且含 `notification_log`（`make db-apply` 已落地；若 test PG 缺表，implementer 跑 `bash scripts/k8s/apply-db.sh` 或对 test PG 应用 `11-notification-channels.sql`）。
- **portal URL 测试**断言**完整 absolute URL**（如 `http://auth.apihub-system/v1/auth/account`），既含 `/v1/` 又无 `/v1/v1/`。
- 每任务 commit；分支 `fix/r2d-gdpr-erasure`；python/pytest 走 `.venv/bin/python`；GateGuard 首编辑每文件拦（陈述 facts 重试）。

---

## Task 1: auth identity — anonymize 扩 notification_log + withdraw 委托 + 测试

**Files:**
- Modify: `services/services/auth/src/auth/identity.py`（`anonymize_user` ~:144-188、`withdraw_consent` ~:217-231）
- Test: `services/services/auth/tests/test_identity.py`（加 notification_log scrub 测试）

**Interfaces:**
- Produces: `anonymize_user` 现多 scrub notification_log；`withdraw_consent` 委托 anonymize_user（签名不变，下游 routes/test 不破）。

- [ ] **Step 1: 改 `anonymize_user`（取 email + 清 notification_log）**

`identity.py` 现状（:150-162）`SELECT id` + scrub。改为：
```python
row = await conn.fetchrow(
    "SELECT id, email FROM user_account WHERE id = $1", user_id,
)
if not row:
    raise ApiError(ErrorCode.NOT_FOUND, "user not found", http_status=404)
old_email = row["email"]

anonymized_email = f"deleted-{secrets.token_hex(8)}@anonymized"
await conn.execute(
    "UPDATE user_account SET email=$1, phone='', name='Deleted User',"
    " password_hash='', status='deleted', updated_at=NOW() WHERE id=$2",
    anonymized_email, user_id,
)
await conn.execute("DELETE FROM tenant_member WHERE user_id = $1", user_id)
await conn.execute("DELETE FROM user_consent WHERE user_id = $1", user_id)
# GDPR erasure：清该用户邮箱作为收件人的投递日志（notification_log.recipient = email PII）
if old_email:
    await conn.execute(
        "DELETE FROM notification_log WHERE recipient = $1", old_email,
    )
```
（其余 api_key 吊销 + Redis 清理不动。）

- [ ] **Step 2: 改 `withdraw_consent` 委托 anonymize_user**

`identity.py` 现状（:217-231）替换为：
```python
async def withdraw_consent(*, user_id: str) -> None:
    """撤回所有同意 → 触发账号匿名化（GDPR right-to-erasure）。

    撤回即擦除（与 delete_account 等效 erasure，不同语义入口）。
    """
    await anonymize_user(user_id=user_id)
    log.info("consent_withdraw_triggered_erasure", user_id=user_id)
```

- [ ] **Step 3: 加测试 `test_anonymize_user_scrubs_notification_log`**

`test_identity.py` 末尾加（镜像既有 `test_anonymize_user_hides_pii` 的真 PG 模式）：
```python
@pytest.mark.asyncio
async def test_anonymize_user_scrubs_notification_log(fake_redis):
    """anonymize 清该用户邮箱作为 recipient 的 notification_log，干扰行（他人 email）留。"""
    from apihub_core import db as db_mod

    user = await identity.create_user(
        email="scrub@example.com", password="secret123", phone="139", name="Scrub"
    )
    uid = user["user_id"]

    async with db_mod.admin_db_session() as conn:
        await conn.execute(
            "INSERT INTO notification_log (id, tenant_id, template_code, channel_type,"
            " recipient, status) VALUES ($1,$2,$3,$4,$5,$6), ($7,$8,$9,$10,$11,$12)",
            "nl_a", "external-public", "task_complete", "email", "scrub@example.com", "sent",
            "nl_b", "external-public", "task_complete", "email", "other@example.com", "sent",
        )
        before = await conn.fetchval(
            "SELECT COUNT(*) FROM notification_log WHERE recipient=$1", "scrub@example.com",
        )
    assert before == 1

    await identity.anonymize_user(user_id=uid)

    async with db_mod.admin_db_session() as conn:
        after_target = await conn.fetchval(
            "SELECT COUNT(*) FROM notification_log WHERE recipient=$1", "scrub@example.com",
        )
        after_other = await conn.fetchval(
            "SELECT COUNT(*) FROM notification_log WHERE recipient=$1", "other@example.com",
        )
    assert after_target == 0
    assert after_other == 1
```
> 若 `UndefinedTableError: notification_log` → test PG 缺表，跑 `bash scripts/k8s/apply-db.sh`（或对该 PG 应用 `scripts/init-db/11-notification-channels.sql`）后重试。为避免污染其他测试，加 `try/finally` 清理插入的 nl_a/nl_b（或依赖 user 邮箱已匿名使残留不可触发——但仍建议显式清理）。

- [ ] **Step 4: 跑 auth 测试 + lint**

```bash
cd services/services/auth && .venv/bin/python -m pytest tests/test_identity.py -v
cd /home/applo/project/ai-system-arch && .venv/bin/python -m pytest services/services/auth/tests/test_routes.py -v
ruff check services/services/auth && mypy services/services/auth
```
Expected: 全 PASS（含新 scrub 测试 + 既有 anonymize/withdraw/delete_account）；withdraw 端点测试（test_routes.py:502，monkeypatch withdraw_consent）不破。

- [ ] **Step 5: Commit**

```bash
git add services/services/auth/src/auth/identity.py services/services/auth/tests/test_identity.py
git commit -m "feat(r2d): anonymize_user 扩清 notification_log(recipient=email) + withdraw_consent 委托 anonymize"
```

---

## Task 2: portal 4 handler 改走 `_forward` + URL 断言测试

**Files:**
- Modify: `services/services/portal/src/portal/routes.py`（4 handler :68-134）
- Test: `services/services/portal/tests/test_routes.py`（加 URL 断言测试）

**Interfaces:**
- Produces: 4 portal handler 转发到正确 `/v1/auth/...`（不再 404）；`_forward` 复用不变。

- [ ] **Step 1: 改 4 handler 复用 `_forward`**

`routes.py` 现 4 handler（portal_delete_account :68、portal_export_account :85、portal_list_consents :102、portal_withdraw_consents :119）每个改为 `_forward` 模式。以 delete_account 为模板：
```python
@app.delete("/v1/portal/auth/account")
async def portal_delete_account(request: Request):
    """删除账号（需 JWT）。转发到 auth-svc。"""
    require_tenant()
    st, body = await _forward(
        "DELETE", "/v1/auth/account",
        headers={"Authorization": request.headers.get("Authorization", "")},
    )
    if st >= 400:
        raise ApiError(ErrorCode.INTERNAL, body, http_status=st)
    return body
```
其余 3 个同模式，path 分别：
- `portal_export_account` → `GET /v1/auth/account/export`
- `portal_list_consents` → `GET /v1/auth/consent`
- `portal_withdraw_consents` → `POST /v1/auth/consent/withdraw`

删掉各 handler 内联的 `async with httpx.AsyncClient(...) as c: r = await c.<method>(f"{auth_base}/auth/...", headers=...)` + 手写 `r.json()/raise` 块（`_forward` 已封装 request + JSON parse；`st>=400` 由 handler 判定 raise）。

- [ ] **Step 2: 加 URL 断言测试**

`test_routes.py` 加（镜像 `test_forward_composes_correct_auth_url` :196 的 `_FakeClient` + absolute URL 捕获；用 conftest 的 `client` fixture 提供 auth context + Authorization 头，因 4 handler 调 `require_tenant()`）：
```python
async def test_portal_account_endpoints_forward_with_v1(client, monkeypatch):
    """M4 回归：4 个 account/consent handler 必须转发到 /v1/auth/...（旧实现缺 /v1 → 404）。"""
    import httpx as _httpx
    captured = {}

    class _FakeResp:
        status_code = 200
        def json(self): return {"ok": True}

    class _FakeClient:
        def __init__(self, *a, **kw): pass
        async def request(self, method, url, **kw):
            captured.setdefault("calls", []).append((method, url))
            return _FakeResp()
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return False

    monkeypatch.setattr(_httpx, "AsyncClient", _FakeClient)

    await client.delete("/v1/portal/auth/account")
    await client.get("/v1/portal/auth/account/export")
    await client.get("/v1/portal/auth/consent")
    await client.post("/v1/portal/auth/consent/withdraw")

    methods_paths = captured["calls"]
    expected = [
        ("DELETE", "http://auth.apihub-system/v1/auth/account"),
        ("GET", "http://auth.apihub-system/v1/auth/account/export"),
        ("GET", "http://auth.apihub-system/v1/auth/consent"),
        ("POST", "http://auth.apihub-system/v1/auth/consent/withdraw"),
    ]
    assert methods_paths == expected, methods_paths
    assert all("/v1/v1/" not in u for _, u in methods_paths)
```
> `client` fixture（conftest）已 monkeypatch `authenticate_request` → TenantContext + 带 Authorization 头，满足 `require_tenant()` + handler 透传。implementer 核 conftest `client` fixture 确切形态。

- [ ] **Step 3: 跑 portal 测试 + lint**

```bash
cd services/services/portal && .venv/bin/python -m pytest tests/ -v
ruff check services/services/portal && mypy services/services/portal
```
Expected: 全 PASS（含新 URL 断言 + 既有 `_forward`/login/register 等）。

- [ ] **Step 4: Commit**

```bash
git add services/services/portal/src/portal/routes.py services/services/portal/tests/test_routes.py
git commit -m "fix(r2d): portal account/consent 4 handler 改走 _forward(/v1/...) 修 M4 缺-v1 404 + URL 断言测试"
```

---

## Task 3: kind e2e 验证（轻量）

**Files:** 无新代码（真入口验证）。

- [ ] **Step 1: 重建 auth + portal 镜像 + load + rollout**

```bash
for s in auth portal; do
  docker build -f services/services/$s/Dockerfile -t registry.apihub.internal/apihub/$s:0.1.0-dev .
  kind load docker-image registry.apihub.internal/apihub/$s:0.1.0-dev --name apihub
  kubectl -n apihub-system rollout restart deploy/$s
done
kubectl -n apihub-system rollout status deploy/auth --timeout=180s
kubectl -n apihub-system rollout status deploy/portal --timeout=180s
make db-apply   # 确保 notification_log 在 kind PG
```

- [ ] **Step 2: withdraw → anonymize → notification_log 清**

可降级直打 auth（跳过 portal 转发层，portal /v1 已被 Task 2 单测覆盖）：
```bash
kubectl -n apihub-system port-forward svc/auth 18002:80 &
# 取/造一个 external-public 用户 JWT，或用 dev bypass 登录
# 预置 notification_log(recipient=<该用户 email>)
docker exec apihub-pg psql -U apihub -d apihub -c "INSERT INTO notification_log (id,tenant_id,template_code,channel_type,recipient,status) VALUES ('nl_e2e','external-public','task_complete','email','<user_email>','sent');"
curl -sf -X POST http://127.0.0.1:18002/v1/auth/consent/withdraw -H "Authorization: Bearer <jwt>"
docker exec apihub-pg psql -U apihub -d apihub -tAc "SELECT count(*) FROM notification_log WHERE recipient='<user_email>';"
```
Expected: withdraw 200；该 recipient notification_log count=0；user_account.status='deleted'。

- [ ] **Step 3: commit（若有 e2e 小修，否则无）**

---

## Self-Review

1. **Spec 覆盖**：withdraw→anonymize(T1)✓ notification_log scrub(T1)✓ portal /v1 + 测试(T2)✓ e2e(T3)✓。
2. **占位符**：无；代码块完整。
3. **类型一致**：`anonymize_user(*, user_id)` / `withdraw_consent(*, user_id)` / `_forward(method, path, **kw)` 签名均不变。
4. **测试根基**：auth 真 PG（fake_redis）+ portal httpx mock absolute URL 断言，均镜像既有测试。

## Execution Handoff

Plan saved to `docs/superpowers/plans/2026-07-18-r2d-gdpr-erasure.md`. 执行选项：
1. **Subagent-Driven（推荐）**
2. **Inline Execution**
