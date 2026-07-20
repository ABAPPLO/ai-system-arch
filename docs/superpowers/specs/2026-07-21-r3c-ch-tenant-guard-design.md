# R3c CH 租户隔离护栏 — 设计 spec

> 日期：2026-07-21
> 阶段：APIHub fix-program · Wave 3 · R3c
> 关联：[fix-program spec](2026-07-15-apihub-fix-program-design.md) §9-E、[phase4 审计](../../phase4-audit-findings.md) §4/§9-E、[R3b clickhouse.py](2026-07-19-r3b-multi-region-design.md)（`query_union_peer` M-2 守卫）、`docs/04-data-model.md` §RLS、`docs/11-multi-tenant.md`
> base：main=`9d52bf6`（R3b #61 合后）
> 依赖：无

## 1. 目标与范围

把 ClickHouse 租户隔离从「应用自觉加 `WHERE tenant_id`」（软约束，审计 §9-E）升级为「`ch_session`/`query_*` 集中强制」（中央多租户不变量贯穿到分析存储的 app 层）。**零 caller 迁移**。

**范围决策（用户 2026-07-21 拍板）**：
- **强制层级**：app-level `ch_session` 校验器（非 DB-level 参数化视图；DB-level 列 future hardening，§5）。
- **校验机制**：字串校验 `%(tenant_id)s` token + `params["tenant_id"]` 绑定 `ctx.tenant_id`（防伪），零迁移（trace 现状已用 `%(tenant_id)s` + `_build_where`）。

**非目标（§5）**：DB-level 参数化视图+revoke（future hardening）、operator direct-CH 保护（app 层范畴外）、per-tenant CH user（不实际）。

## 2. 现状（审计 §9-E + 核实）

- `trace/repository.py` 已有 `_build_where(query, viewer_tenant_id)` 烘 `WHERE tenant_id = %(tenant_id)s`，admin 走 `force_tenant_id=None`。**现状查询已正确过滤**——但 `ch_session`/`query_all`/`query_one` **信任 SQL**：未来查询漏 `_build_where`/`viewer_tenant_id` → 跨租户泄露（§9-E gap）。
- CH 24.3（`docker-compose.dev.yml:113` + multi-region compose）：支持参数化视图；但 **row policy 不适配共享 CH user 模型**（`USING` 只能引用列 + `currentUser()`，无 per-request tenant var → 需每租户一 CH user，不实际）。故 DB-level = 参数化视图+revoke（重，defer）；app-level = 校验器（中量，本轮）。
- `init-clickhouse/01-schema.sql:2` 注释「ClickHouse 不做 RLS（无 tenant 隔离），靠 SQL WHERE」——无 views/policies。

## 3. 设计

### 3.1 校验契约（`_assert_tenant_filter`）

在 `services/libs/apihub-core/src/apihub_core/clickhouse.py` 加 `_assert_tenant_filter(sql, params, force_tenant_id)`，wire 进 `query_all`/`query_one`（`ch.query()` 前）。helper 自行解析 + 校验（query_all/query_one 只传 `force_tenant_id`，不预解析）：

1. **解析有效 tenant_id**：`force_tenant_id="sentinel"` → `ctx = get_tenant_context()`；`ctx is None` → `raise RuntimeError("ch_session called without tenant context")`（沿用现有 ch_session 行为）；否则 `ctx.tenant_id`。`force_tenant_id=<str>` → 该 str。`force_tenant_id=None` → **admin 旁路**。
2. **租户作用域**（`force_tenant_id != None`）：
   - **必须** `"%(tenant_id)s" in sql`（param token 存在）→ 否则 `raise ValueError("tenant-scoped CH query missing %(tenant_id)s filter")`。catches「漏 `_build_where`」。注意：`SELECT tenant_id` 列但无 filter 的 SQL 不含 `%(tenant_id)s` token → 也 raise（fail-closed，比查列名可靠）。
   - **必须** `params.get("tenant_id")` 存在且 `==` 有效 tenant_id（**防伪**：caller 不能传别的租户 id）→ 否则 `raise ValueError("tenant_id param does not match context tenant")`。
3. **admin 旁路**（`force_tenant_id=None`）：跳过校验 + `log.info("ch_admin_scope_query", sql=sql[:120])` 审计（跨租户 admin 查询可追溯）。

### 3.2 强制点

`query_all` + `query_one`（app 查询入口；trace 全走这俩 + `query_union_peer`）。`query_union_peer` 已被 R3b S4 fix **M-2 守卫**锁 admin-only（`peer_sql + force_tenant_id != None → ValueError`），不需再加校验。

`ch_session` yield 原始 `_client`；直连 `with ch_session() as ch: ch.query(...)` 标 **internal-only**（仅在 clickhouse.py 自己的 helper 内；文档化）。operator direct-CH 不在 app 层范畴（§5）。

### 3.3 数据流

```
trace query → query_all(sql, params, force_tenant_id="sentinel")
             → _assert_tenant_filter（token + 绑定 ctx）→ 通过则 ch.query
admin       → query_all(sql, None, force_tenant_id=None)
             → 跳过校验 + 审计 log → ch.query
```

## 4. 测试

新 `services/libs/apihub-core/tests/test_ch_tenant_guard.py`（mock `_client`，同 `test_multi_region_ch.py` 风格）：
- `test_tenant_scope_missing_token_raises`：`query_all("SELECT * FROM t WHERE ts>%(s)s", {"s":...}, force_tenant_id="sentinel")` ctx=t_a → raise（漏 `%(tenant_id)s`）。
- `test_tenant_scope_spoofed_tenant_raises`：`query_all("...WHERE tenant_id=%(tenant_id)s...", {"tenant_id":"t_b"}, force_tenant_id="sentinel")` ctx=t_a → raise（params tenant_id≠ctx，防伪）。
- `test_tenant_scope_valid_passes`：同上但 `params["tenant_id"]="t_a"`==ctx → 跑通。
- `test_admin_opt_out_no_validation_audit`：`query_all("SELECT * FROM t", None, force_tenant_id=None)` → 跑通（不校验）+ 断言 `log.info("ch_admin_scope_query", ...)` 被调。
- `test_query_union_peer_still_admin_only`：regression——M-2 守卫仍 `peer_sql+force_tenant_id!=None→ValueError`（R3b 已覆盖，确认不回归）。

**回归**：`pytest services/services/trace/tests/`（trace 查询用 `%(tenant_id)s`+`_build_where`→契约满足→不回归）+ `services/libs/apihub-core/tests/`（含 S4 `test_multi_region_ch`，`query_union_peer` 用 `force_tenant_id=None` admin→不触发校验→不回归）。

**无 kind e2e**——校验器是 app 层逻辑，单测 mock `_client` 足够；live CH 不验证校验逻辑。

## 5. Out-of-scope（本轮不做）

- DB-level 参数化视图+revoke（真 store-level RLS-equivalent；future hardening）。
- operator direct-CH 保护（app 层范畴外；operator 须走 app 或受 DB-level 视图保护，后者 defer）。
- per-tenant CH user（row-policy 路线，不实际）。

## 6. 任务

- **T1**：`_assert_tenant_filter` helper + wire 进 `query_all`/`query_one` + admin 审计 log + 5 单测（TDD RED→GREEN）+ `init-clickhouse/01-schema.sql` 注释更新。
- **T2**：全回归（trace + apihub-core + S4 `test_multi_region_ch` 不回归）+ final opus whole-branch review → squash-PR。

## 7. 参考

- [fix-program spec](2026-07-15-apihub-fix-program-design.md) §9-E
- [phase4 审计](../../phase4-audit-findings.md) §4 / §9-E
- [R3b spec](2026-07-19-r3b-multi-region-design.md)（`clickhouse.py` `query_union_peer` M-2 守卫）
- `docs/04-data-model.md` §RLS、`docs/11-multi-tenant.md`
- 关联记忆：[[apihub-fix-program-progress]] [[apihub-audit-2026-07-15]]
