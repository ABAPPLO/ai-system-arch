# R2d spec — GDPR erasure 闭环（withdraw→anonymize + notification_log scrub + portal /v1）

日期：2026-07-18 · 分支 `fix/r2d-gdpr-erasure`（已建，base main=218a8b2）· 依据：fix-program 设计 §5 Wave 2 R2d（引用 §3.5/3.6）+ M4 预存 bug（memory 记录）。

## 问题

两处 GDPR 链路断裂 + 一处 R2b 遗留 PII 覆盖：

1. **`withdraw_consent` 没接 `anonymize_user`（docstring 撒谎）** —— `auth/identity.py:217` 的 `withdraw_consent` docstring 写「撤回所有同意 → 触发账号匿名化」，但实现只 `UPDATE user_consent SET status='withdrawn'`，**从不调 `anonymize_user`**。用户撤回同意 ≠ 触发擦除，违反 GDPR right-to-erasure 链路。`anonymize_user`（:144）已存在且完整（scrub user_account + 删 tenant_member/user_consent + 吊销 api_key + 清 Redis），只是 withdraw 没接上。

2. **portal 4 个转发 handler URL 缺 `/v1`（M4 预存 bug，生产 404）** —— `portal/routes.py:68-134` 的 `portal_delete_account`/`portal_export_account`/`portal_list_consents`/`portal_withdraw_consents` 手拼 `f"{auth_base}/auth/account"`，但 `auth_base`（:21）已 `rsplit("/",3)` 砍掉 `/v1/apikey/verify` 得到无 `/v1` 的 base（`http://auth.apihub-system`）→ 实际打 `/auth/account`，而 auth 真路由是 `/v1/auth/account`（`auth/routes.py:284`）→ 生产 404。同文件 R0c 新 handler 用 `_forward(method, "/v1/auth/...")`（:23）是对的；这 4 个 M4 旧 handler 没走 `_forward`，且**无 URL 断言测试**（既有 `test_forward_composes_correct_auth_url` 只覆盖 `_forward` 本身，没覆盖这 4 个 handler）。

3. **`notification_log.recipient` PII 未被 erasure 覆盖（R2b 遗留）** —— R2b 加的 `notification_log(per-tenant)` 的 `recipient` 字段对 email 渠道是用户邮箱（PII）；`anonymize_user` 现有 scrub 范围（user_account/tenant_member/user_consent/api_key/Redis）不含它，擦除后用户邮箱仍残留在投递日志里。

## 范围（已与用户确认 = 选项 A）

- **withdraw→anonymize**：`withdraw_consent` 委托 `anonymize_user`（兑现 docstring + roadmap）。
- **扩 anonymize scrub notification_log**：按 `recipient = 旧 email` 清（R2b 刚加的 email PII）。
- **portal 4 handler 改走 `_forward("/v1/...")`** + 补 URL 断言测试（镜像 `test_forward_composes_correct_auth_url`）。
- erasure 审计依赖既有 `admin_db_session` 写 audit_log（R0a 已落地，db.py `_write_admin_audit`）。

## 不做（R2d 边界）

- **audit_log.actor_id / ClickHouse 调用事件的 PII sweep**（选项 C，跨 CH/异步，偏大，留后续）。
- **withdraw 保留 consent 审计快照**——`anonymize_user` 已删 user_consent 且 admin_db_session 写 audit_log，erasure 事实有审计；不为「撤回前状态」额外留快照。
- **notification_log scrub 改走 Kafka 事件 → notification 消费**（更尊重聚合所有权，但 async + 重，留后续；见下「所有权」决策）。
- 不动 delete_account/export_account/consent 端点的 auth 侧实现（它们已对）。

## 设计

### ① `auth/identity.py` — `anonymize_user` 扩 notification_log scrub

现状 `:150-153` 只 `SELECT id FROM user_account WHERE id=$1`（存在性）。改为同时取 email（擦除前留存，用于按 recipient 清 notification_log）：

```python
row = await conn.fetchrow(
    "SELECT id, email FROM user_account WHERE id = $1", user_id,
)
if not row:
    raise ApiError(ErrorCode.NOT_FOUND, "user not found", http_status=404)
old_email = row["email"]
```

在现有 `DELETE FROM user_consent ...`（:164）之后、Redis 清理之前，加（同 admin_db_session 事务内）：

```python
# GDPR erasure：清该用户邮箱作为收件人的投递日志（notification_log.recipient = email PII）
if old_email:
    await conn.execute(
        "DELETE FROM notification_log WHERE recipient = $1", old_email,
    )
```

> **聚合所有权决策**：`notification_log` 的拥有服务是 notification（§9-B）。auth 的 `anonymize_user` 经 `admin_db_session` 跨租户删它，**可接受**，因为：(a) `anonymize_user` 本就是跨聚合擦除操作（现已删 tenant_member/user_consent/api_key —— identity/tenant/app 多聚合）；(b) GDPR right-to-erasure 天然跨聚合；(c) §9-B「BFF 不得直写领域表」针对 BFF（portal），非擦除权威；(d) `admin_db_session` 是此类平台级操作的指定旁路 + 写 audit_log。更尊重所有权的替代（Kafka `user.anonymized` 事件 → notification 消费自删）留后续。spec 显式记录此权衡。

### ② `auth/identity.py` — `withdraw_consent` 委托 `anonymize_user`

现状 `:217-231`：存在性检查 + `UPDATE user_consent status='withdrawn'` + log。改为委托（anonymize 已含存在性检查 + 删 user_consent + 全量 scrub）：

```python
async def withdraw_consent(*, user_id: str) -> None:
    """撤回所有同意 → 触发账号匿名化（GDPR right-to-erasure）。

    撤回即擦除（与 delete_account 等效 erasure，不同语义入口）。
    """
    await anonymize_user(user_id=user_id)
    log.info("consent_withdraw_triggered_erasure", user_id=user_id)
```

（删掉原 UPDATE——anonymize 已 `DELETE FROM user_consent`，UPDATE 冗余。）

### ③ `portal/routes.py` — 4 handler 改走 `_forward`

4 个 handler（:68-134）改为复用 `_forward(method, path, **kw)`（:23），path 带 `/v1`，透传 `Authorization` 头，统一 `st>=400 → ApiError(INTERNAL)`：

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

其余 3 个（export_account→`GET /v1/auth/account/export`、list_consents→`GET /v1/auth/consent`、withdraw_consents→`POST /v1/auth/consent/withdraw`）同模式。删掉各 handler 内联的 `httpx.AsyncClient` + 手拼 URL + 手写 error parse（`_forward` 已封装）。

### ④ 测试

- **`auth/tests/test_identity.py`**：扩 `test_anonymize_user_hides_pii`（或新 `test_anonymize_user_scrubs_notification_log`）——fake DB 预置 `notification_log` 行 recipient=用户 email + 干扰行（他人 email），断言 anonymize 后该用户行删、干扰行留。沿用既有 fake DB 模式（implementer 核 `test_identity.py` 的 _FakeConn/stub 写法）。
- **`portal/tests/test_routes.py`**：加 4 个 URL 断言测试，镜像 `test_forward_composes_correct_auth_url`（:196）——mock httpx，断言每个 handler 打的 absolute URL 精确含 `/v1/auth/...`（如 `http://auth.apihub-system/v1/auth/account`），专抓 M4 的缺-/v1 回归。

## 改动清单

- `services/services/auth/src/auth/identity.py`：`anonymize_user`（SELECT email + DELETE notification_log）+ `withdraw_consent`（委托）。
- `services/services/auth/tests/test_identity.py`：notification_log scrub 断言。
- `services/services/portal/src/portal/routes.py`：4 handler 改 `_forward`。
- `services/services/portal/tests/test_routes.py`：4 个 URL 断言测试。

## 验证（走真实入口）

- **单测**：`pytest services/services/auth/tests services/services/portal/tests -v` 全绿（含新断言）；`ruff check` + `mypy` 过。
- **kind e2e**（可选，小轮可裁）：portal `POST /v1/portal/auth/consent/withdraw`（带 dev JWT）→ auth withdraw_consent → anonymize_user → 确认 user_account.status='deleted'、notification_log 该 recipient 行清、返回 200。portal 4 端点不再 404。

## 风险

- **withdraw=erasure 语义激进**：撤回同意 = 全量擦除（账号 deleted、api_key 吊销）。这是 docstring 既定意图 + roadmap 明示，但比「撤回仅停止处理」重。spec 显式声明（撤回即擦除，与 delete_account 等效 erasure）。若产品后续要「软撤回」，再分。
- **notification_log 跨聚合写**：见上「所有权决策」（admin_db_session 擦除权威，可接受；Kafka 替代留后续）。
- **既有 withdraw 测试**：`test_routes.py:502` monkeypatch `withdraw_consent`（验端点调用它）——端点行为不变（仍调 withdraw_consent），该测试不破。`test_identity.py` 若有 withdraw_consent 内部单测（grep 未见），implementer 核并随语义更新。
- **`old_email` 为空/NULL**：`if old_email:` 守卫，空则跳过 notification_log scrub（无 email 无 PII 可清）。

## 依赖

无前置硬依赖。R2b 的 notification_log 表已在（apply-db 落地）。

## 与后续轮次关系

- 选项 C（audit_log.actor_id / CH PII sweep）留后续 GDPR 深化轮。
- notification_log scrub 的 Kafka 事件化（尊重聚合所有权）留 §9-B 深化。
