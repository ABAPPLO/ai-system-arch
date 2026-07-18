# R2e — GDPR erasure 合规/正确性收尾（design）

**日期**：2026-07-18
**分支**：`fix/r2e-erasure-followup`（base main = `c910de5`，R2d 已合）
**前置**：R2d（PR#55）已 squash-merge。本轮修 R2d 终审浮出、超 R2d 范围的 3 条遗留。

## 背景

R2d 实现了 GDPR erasure 闭环（withdraw→anonymize + notification_log scrub + portal /v1），但终审 opus whole-branch review 浮出 3 条超出 R2d 范围的合规/正确性问题（记在 `.superpowers/sdd/progress.md` §Deferred compliance/correctness findings）。本轮逐条修。

## 关键事实（探索确认）

- `admin_db_session(*, audit_reason: str | None = None)`（`apihub_core/db.py:198`）：审计 opt-in，`reason = audit_reason or _audit_reason_var.get()`（L216），仅当有 reason 才 `_write_admin_audit(reason)`（L229）。`anonymize_user` 当前调 `admin_db_session()` 不传 reason、路由也不 set_audit_reason → **erasure 不落任何 audit_log**。
- `app` 表（`init-db/01-schema.sql:62`）与 `api_key` 表（:76）**仅有 tenant_id，无 user 归属字段**（无 owner_user_id / created_by）。external-public 是所有外部用户共享租户 → "按用户收窄 api_key scrub" 在当前 schema 下**不成立**。
- `billing_record` 真实列（`init-db/05-billing.sql:52`）：`tenant_id / id / subscription_id / period_start / period_end / call_count / token_count / base_charge_cents / overage_charge_cents / total_charge_cents / status / invoice_url / created_at`。`export_user_data` 当前 SELECT 的 `period / plan_name / total_calls / total_tokens / base_cents / overage_cents` **全部不存在** → export 直接抛错（`test_export_user_data_returns_account` / `_includes_tenants` 2 个红）。
- 测试影响面（`test_identity.py`）：无任何测试断言 api_key 被 anonymize 吊销；无测试断言 export 含 apps/api_keys/billing_records。→ 3 条改动均不破现有测试。

## 语义决策（已与用户对齐）

external-public 租户的 app / api_key / billing_record 是**租户级共享资源**，不携带个人身份、不归属单个用户。GDPR erasure / portability 的对象是**该数据主体本人的个人数据**。因此：

- erasure 不应动租户级 key（否则误伤其他用户 = DoS）。
- export 不应包含租户级共享数据（无法按 user 收窄，且非个人数据）。

## 改动（全部在 `services/services/auth/src/auth/identity.py`）

### #1 erasure 写审计

`anonymize_user`（identity.py:152）：`db.admin_db_session()` → `db.admin_db_session(audit_reason="gdpr_erasure")`。

- 一处改动，覆盖 withdraw_consent / delete_account / 所有调用路径。
- audit_log 经 `_write_admin_audit` 写入：独立 raw 连接、best-effort、不影响本事务、不递归。

### #2 删 api_key scrub（erasure 不吊销租户级 key）

删 identity.py:174-182：

```python
apps = await conn.fetch("SELECT id FROM app WHERE tenant_id = $1", EXTERNAL_PUBLIC_TENANT)
for app in apps:
    await conn.execute(
        "UPDATE api_key SET status='revoked', revoked_at=NOW()"
        " WHERE app_id=$1 AND status='active'",
        app["id"],
    )
```

- 更新 `anonymize_user` docstring：去掉"API key 吊销"，操作链改为「user_account 匿名化 → tenant_member 删除 → user_consent 删除 → notification_log 清理 → Redis 清理」，补注"租户级 api_key 不动（无 user 归属，erasure 仅清个人 PII）"。

### #3 export 砍租户级三块

`export_user_data`（identity.py:234）：删 apps / api_keys / billing_records 三块查询与 return dict 对应字段。return 简化为：

```python
return {
    "user_id": row["id"],
    "exported_at": datetime.now(timezone.utc).isoformat(),
    "account": {...},   # 不变
    "tenants": [...],   # 不变
}
```

- 清理因此变未用的引用（`EXTERNAL_PUBLIC_TENANT` 若 export 不再用但 anonymize 仍用则保留；`maybe_decrypt` 仍用于 account.phone/name，保留）。

## 测试（TDD，repo-root `.venv/bin/python -m pytest`，py3.11）

- **#1** 新增 `test_anonymize_writes_audit_log`：anonymize 前后查 audit_log，断言多一条（reason 关联 gdpr_erasure）。需真 PG（db_pool fixture）。
- **#2** 新增 `test_anonymize_does_not_revoke_api_keys`：预置一个 active api_key（external-public 某 app），anonymize 后断言该 key 仍 active。锁住"erasure 不动租户级 key"语义。
- **#3** 新增 `test_export_excludes_tenant_scoped_data`：断言 export 返回 dict 无 `apps`/`api_keys`/`billing_records` key。原 `test_export_user_data_returns_account` + `_includes_tenants` 转绿（不再触发 billing 查询）。
- ruff 0 new；mypy clean。
- **kind e2e**（增量小、保险）：复用 R2d T3 脚本，register→verify→login→consent/withdraw → 断言 erasure 链不回归 + audit_log 落库 + api_key 未被动。

## 范围 / 非范围

**范围**：`identity.py` 的 `anonymize_user`（#1 audit + #2 删 scrub）与 `export_user_data`（#3 砍三块）+ `test_identity.py` 对应测试。

**非范围**（留后续）：

- `app.owner_user_id` schema 改（若未来要真按 user 收窄 scrub/export）。
- withdraw_consent 路由的 contextvar audit（#1 已在 anonymize_user 统一覆盖，无需路由再设）。
- export 是否补"用户创建的 app"维度（依赖 owner schema）。

## 风险

- 删 api_key scrub 是**行为改变**（之前会吊销，现在不吊）。若有外部流程依赖"删用户→其 key 失效"，需注意——但当前 key 本就不绑 user，此依赖本就不成立。新增 `test_anonymize_does_not_revoke_api_keys` 显式锁住新语义。
- audit 写入 best-effort：若 audit_log 写失败，erasure 仍成功（不回滚）。可接受（审计不应阻塞 erasure）。
