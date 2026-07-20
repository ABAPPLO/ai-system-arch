# R3c CH 租户隔离护栏 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 CH 租户隔离从「应用自觉加 WHERE」（软约束）升级为 `query_all`/`query_one` 集中强制（`%(tenant_id)s` token 检 + `params["tenant_id"]` 绑定 ctx 防伪 + admin 旁路审计），零 caller 迁移。

**Architecture:** app-level 校验器 `_assert_tenant_filter(sql, params, force_tenant_id)` 在 `query_all`/`query_one` 的 `ch.query()` 前调用。租户作用域（`force_tenant_id != None`）必须含 `%(tenant_id)s` token + `params["tenant_id"]`==解析出的 tenant_id；admin（`force_tenant_id=None`）旁路 + 审计 log。`query_union_peer` 已被 R3b M-2 守卫锁 admin-only，不加。

**Tech Stack:** Python 3.11 / clickhouse-connect / pytest (mock `_client`) / repo-root `.venv/bin/python -m pytest`.

**Spec:** `docs/superpowers/specs/2026-07-21-r3c-ch-tenant-guard-design.md`（分支 `fix/r3c-ch-tenant-guard` @ `43fc6ab`）。

## Global Constraints

- **校验契约**：租户作用域（`force_tenant_id="sentinel"` 或 str）→ SQL 须含 `%(tenant_id)s` token + `params.get("tenant_id")`==解析出的 tenant_id（防伪），否则 `ValueError`。admin（`force_tenant_id=None`）→ 旁路 + `log.info("ch_admin_scope_query", sql=sql[:120])`。
- **零 caller 迁移**：trace 现状已用 `%(tenant_id)s` + `_build_where` → 契约满足 → 不改 trace。
- **强制点**：`query_all` + `query_one`（`query_one` 调 `query_all` → 自动覆盖）。`query_union_peer` 不动（M-2 已 admin-only）。
- **pytest runner**：repo-root `.venv/bin/python -m pytest <path> -v`（py3.11）。NOT `services/.venv`。
- **GateGuard**：first bash + first edit per file block once — state 2 facts then retry；disable `ECC_GATEGUARD=off`.
- 每任务一个 commit；分支 `fix/r3c-ch-tenant-guard`；最终一个 squash-PR。

## File Structure

**新建**：
- `services/libs/apihub-core/tests/test_ch_tenant_guard.py` — 5 单测（mock `_client` + `get_tenant_context`，同 `test_multi_region_ch.py` 风格）

**修改**：
- `services/libs/apihub-core/src/apihub_core/clickhouse.py` — 加 `_assert_tenant_filter` helper（~line 114，`query_all` 前）+ wire 进 `query_all`（line 125 前）
- `scripts/init-clickhouse/01-schema.sql` — 顶部注释（line 2）更新

---

## Task 1: `_assert_tenant_filter` + wire + 5 单测 + init-clickhouse 注释

**Files:**
- Modify: `services/libs/apihub-core/src/apihub_core/clickhouse.py`（加 `_assert_tenant_filter` ~line 114；wire 进 `query_all` line 125）
- Modify: `scripts/init-clickhouse/01-schema.sql:2`（注释更新）
- Test: `services/libs/apihub-core/tests/test_ch_tenant_guard.py`（create）

**Interfaces:**
- Consumes: `get_tenant_context`（`apihub_core.tenant`，已 import line 16）；`log`（line 18，已 `get_logger(__name__)`）。
- Produces: `_assert_tenant_filter(sql: str, params: dict | None, force_tenant_id: str | None) -> None`（raise `ValueError` on 违约；admin 旁路 log.info）。

- [ ] **Step 1: 写失败测试 `services/libs/apihub-core/tests/test_ch_tenant_guard.py`**

```python
from unittest.mock import MagicMock, patch

from apihub_core import clickhouse as ch


def _set_client():
    ch._client = MagicMock()
    ch._peer_client = None
    r = MagicMock(); r.column_names = ("c",); r.result_rows = [(1,)]
    ch._client.query.return_value = r


def test_tenant_scope_missing_token_raises():
    _set_client()
    with patch("apihub_core.clickhouse.get_tenant_context") as gtc:
        gtc.return_value = MagicMock(tenant_id="t_a")
        try:
            ch.query_all("SELECT * FROM t WHERE ts>%(s)s", {"s": "x"},
                          force_tenant_id="sentinel")
            assert False, "expected ValueError (missing %(tenant_id)s)"
        except ValueError as e:
            assert "%(tenant_id)s" in str(e)


def test_tenant_scope_spoofed_tenant_raises():
    _set_client()
    with patch("apihub_core.clickhouse.get_tenant_context") as gtc:
        gtc.return_value = MagicMock(tenant_id="t_a")
        try:
            ch.query_all("SELECT * FROM t WHERE tenant_id=%(tenant_id)s",
                         {"tenant_id": "t_b"}, force_tenant_id="sentinel")
            assert False, "expected ValueError (spoofed tenant_id)"
        except ValueError as e:
            assert "tenant_id param does not match" in str(e)
        # 防伪：即使 token 存在，params tenant_id ≠ ctx → raise


def test_tenant_scope_valid_passes():
    _set_client()
    with patch("apihub_core.clickhouse.get_tenant_context") as gtc:
        gtc.return_value = MagicMock(tenant_id="t_a")
        rows = ch.query_all("SELECT c FROM t WHERE tenant_id=%(tenant_id)s",
                            {"tenant_id": "t_a"}, force_tenant_id="sentinel")
        assert rows == [{"c": 1}]


def test_admin_opt_out_no_validation_audit():
    _set_client()
    with patch("apihub_core.clickhouse.get_tenant_context") as gtc:
        gtc.return_value = MagicMock(tenant_id="t_a")  # admin 不读 ctx，但设防意外
        with patch.object(ch.log, "info") as log_info:
            rows = ch.query_all("SELECT * FROM t", None, force_tenant_id=None)
            assert rows == [{"c": 1}]
        log_info.assert_any_call("ch_admin_scope_query", sql="SELECT * FROM t")


def test_query_union_peer_still_admin_only():
    _set_client()
    with patch("apihub_core.clickhouse.get_tenant_context") as gtc:
        gtc.return_value = MagicMock(tenant_id="t_a")
        # M-2 守卫：peer_sql + 非 admin → ValueError
        try:
            ch.query_union_peer("SELECT 1", "SELECT 1", None,
                                 force_tenant_id="sentinel")
            assert False, "expected ValueError (M-2 guard)"
        except ValueError as e:
            assert "peer_sql requires admin scope" in str(e)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest services/libs/apihub-core/tests/test_ch_tenant_guard.py -v`
Expected: FAIL — `_assert_tenant_filter` 不存在（query_all 不校验，mock query 直跑 → 不 raise → `assert False` 触发 → 测试 ERROR/Fail）。

- [ ] **Step 3: 加 `_assert_tenant_filter` helper + wire 进 query_all**

在 `clickhouse.py` `current_tenant_id_or_none`（line 112）后、`query_all`（line 115）前加：

```python
def _assert_tenant_filter(
    sql: str,
    params: dict[str, Any] | None,
    force_tenant_id: str | None,
) -> None:
    """CH 租户隔离中央护栏（app-level）。

    租户作用域（force_tenant_id != None）：SQL 须含 `%(tenant_id)s` token +
    params["tenant_id"] 须 == 解析出的 tenant_id（防伪）。admin（None）旁路 + 审计。

    非 store-level（operator direct-CH 不受保护，见 spec §5）；DB-level 参数化视图列 future hardening。
    """
    if force_tenant_id is None:
        log.info("ch_admin_scope_query", sql=sql[:120])
        return
    if force_tenant_id == "sentinel":
        ctx = get_tenant_context()
        if ctx is None:
            raise RuntimeError(
                "ch_session called without tenant context; "
                "pass force_tenant_id=None for admin view"
            )
        effective = ctx.tenant_id
    else:
        effective = force_tenant_id
    if "%(tenant_id)s" not in sql:
        raise ValueError(
            "tenant-scoped CH query missing %(tenant_id)s filter"
        )
    if params is None or params.get("tenant_id") != effective:
        raise ValueError("tenant_id param does not match context tenant")
```

wire 进 `query_all`（line 115-128，在 `with ch_session(...)` 前加一行）：

```python
def query_all(
    sql: str,
    params: dict[str, Any] | None = None,
    *,
    force_tenant_id: str | None = "sentinel",
) -> list[dict[str, Any]]:
    """便捷封装：SELECT 返回 list[dict]。

    ClickHouse 用 %(name)s 风格的参数化（不是 asyncpg 的 $1）。
    租户作用域查询经 _assert_tenant_filter 强制 %(tenant_id)s + 绑定 ctx（防伪）。
    """
    _assert_tenant_filter(sql, params, force_tenant_id)
    with ch_session(force_tenant_id=force_tenant_id) as ch:
        result = ch.query(sql, parameters=params or {})
        cols = result.column_names
        return [dict(zip(cols, row, strict=False)) for row in result.result_rows]
```

`query_one`（line 131-139）调 `query_all` → 自动覆盖，不改。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest services/libs/apihub-core/tests/test_ch_tenant_guard.py -v`
Expected: PASS（5 用例）。

- [ ] **Step 5: 更新 init-clickhouse 注释**

`scripts/init-clickhouse/01-schema.sql:2` 当前：
```sql
-- 注意：ClickHouse 不做 RLS（无 tenant 隔离），靠查询 SQL WHERE tenant_id 过滤
```
改为：
```sql
-- 注意：ClickHouse 不做 DB-level RLS。app 层由 apihub_core.clickhouse._assert_tenant_filter
-- 强制 query_all/query_one 的租户作用域查询含 %(tenant_id)s + params tenant_id 绑定 ctx（防伪）；
-- admin(force_tenant_id=None) 旁路+审计。operator direct-CH 不受保护（DB-level 参数化视图列 future hardening）。
```

- [ ] **Step 6: Commit**

```bash
git add services/libs/apihub-core/src/apihub_core/clickhouse.py services/libs/apihub-core/tests/test_ch_tenant_guard.py scripts/init-clickhouse/01-schema.sql
git commit -m "feat(multi-tenant): ch_session/query_all/query_one 校验 %(tenant_id)s+绑定 ctx（CH 租户隔离 app-level 护栏，R3c）"
```

---

## Task 2: 全回归（trace + apihub-core + S4 test_multi_region_ch 不回归）

**Files:** 无修改（验证 only）

**Interfaces:**
- Consumes: Task 1 的 `_assert_tenant_filter` wired 进 query_all/query_one。

- [ ] **Step 1: trace 回归**

Run: `.venv/bin/python -m pytest services/services/trace/tests/ -v`
Expected: 全 PASS。trace 查询用 `%(tenant_id)s` + `_build_where` → 契约满足 → 不触发 ValueError。若某 trace 查询漏了 `%(tenant_id)s`（即 R3c 抓出 pre-existing 漏过滤）→ 测试 fail（RED）→ 修该 trace 查询加 `%(tenant_id)s`+绑定 ctx（这是 R3c 护栏发挥价值，非回归 bug）。

- [ ] **Step 2: apihub-core 回归（含 S4 test_multi_region_ch + R3b S4 fix 的 query_union_peer M-2/M-4 测试）**

Run: `.venv/bin/python -m pytest services/libs/apihub-core/tests/ -v`
Expected: 全 PASS。`test_multi_region_ch` 的 `query_union_peer` 用 `force_tenant_id=None`（admin）→ 不触发 `_assert_tenant_filter` 的租户校验 → 不回归。`test_apisix_client` 等不涉 CH → 不回归。

- [ ] **Step 3: （optional）auth 回归**

Run: `.venv/bin/python -m pytest services/services/auth/tests/ -v`
Expected: 全 PASS（auth 不调 CH query_all/query_one）。

- [ ] **Step 4: 记录回归结果到 report**

写 `.superpowers/sdd/task-r3c-t2-report.md`：trace/apihub-core/auth 套件结果（passed/skipped 数）+ 任何 R3c 护栏抓出的 pre-existing 漏过滤（若有，列出 file:line + 修法）。

（**final opus whole-branch review + squash-PR** 是 subagent-driven 执行的 terminal steps，不在本 plan task 内——T1+T2 完成后由 controller 调 final reviewer + （用户发话）开 PR。）

---

## Self-Review（对照 spec）

- **Spec 覆盖**：§3.1 `_assert_tenant_filter` 契约（token+绑定+admin 旁路）→ T1 Step 3；§3.2 强制点 query_all/query_one（query_one 经 query_all 覆盖）→ T1 Step 3；query_union_peer M-2 不动 → T1 Step 3 注 + T2 regression test_query_union_peer_still_admin_only；§4 5 单测 → T1 Step 1；§5 out-of-scope（DB-level/operator direct-CH）→ init-clickhouse 注释 Step 5；§6 T1/T2 → 本 plan T1/T2。全覆盖。
- **占位符扫描**：无 TBD/TODO；init-clickhouse 注释是实文。
- **类型一致**：`_assert_tenant_filter(sql: str, params: dict | None, force_tenant_id: str | None) -> None` 在 T1 Step 3 定义 + Step 1 测试调用签名一致；`query_all(*, force_tenant_id="sentinel")` 现有签名不变（wire 只加一行 `_assert_tenant_filter(sql, params, force_tenant_id)`）。

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-21-r3c-ch-tenant-guard.md`. 两种执行方式：

1. **Subagent-Driven（推荐）** — 每 task 派 fresh subagent，task 间 review，快迭代。
2. **Inline Execution** — 本会话内 executing-plans 批量 + checkpoint。

选哪种？
