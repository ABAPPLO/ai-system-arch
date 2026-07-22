# Hygiene (ruff + test_db_rls) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `ruff check services/ scripts/` 30 errors → 0;`test_db_rls.py` PG-up 时 10 fail → 绿。不改运行时行为。

**Architecture:** Task 1 机械+判断修 ruff(逐规则等价改写/靶向 noqa);Task 2 test_db_rls 抽 `_rls_conn()`(superuser 连接 + `SET LOCAL ROLE apihub_app`)让 RLS 真生效,PG up 验证。

**Tech Stack:** Python 3.11 / pytest (asyncio_mode=auto) / ruff / asyncpg / docker compose(dev PG)

**Spec:** `docs/superpowers/specs/2026-07-23-hygiene-ruff-testdbrls-design.md`

## Global Constraints

- **不改变运行时行为**:ruff 修复只做等价改写或靶向 `# noqa: <rule>`(带原因注释);test_db_rls 只改测试连接 role,不改被测的 db/RLS 代码。
- **ruff 范围 = CI 范围**:`ruff check services/ scripts/` 须 0 errors(mypy 非本轮,pre-existing 不计)。
- **PG invariant**:superuser = `apihub`(不可改);app role = `apihub_app`(NOSUPERUSER NOBYPASSRLS,RLS 生效)。test 用 superuser 连 + `SET LOCAL ROLE apihub_app` 退到 app role(免密码)。
- **测试约定**:`asyncio_mode=auto`;逐 suite 跑(避免 basename 碰撞)。
- **每轮一个 squash-PR**;push/merge 仅在用户要求时。

---

## File Structure

| 文件 | 责任 | 动作 |
|---|---|---|
| `services/**` + `scripts/**`(~17 文件,30 ruff errors) | lint 清零 | Modify |
| `services/libs/apihub-core/tests/test_db_rls.py` | RLS 测试退到 app role | Modify |

---

## Task 1: ruff 清零(`ruff check services/ scripts/` 30 → 0)

**Files:** 30 errors 跨 ~17 文件(热点:`tests/test_ch_tenant_guard.py` 5、`scripts/smoke/k8s-ai-gateway-dispatcher.py` 4、`services/services/quota/src/quota/repository.py` 3;余散布)。先 `ruff check services/ scripts/` 拿全量清单。

**Interfaces:** 无(纯 lint)。

- [ ] **Step 1: 拿当前全量清单**

Run: `ruff check services/ scripts/ 2>&1 | tee /tmp/ruff-before.txt`
记录 30 errors 的 file:line:rule。

- [ ] **Step 2: 自动修可 fix 的**

Run: `ruff check --fix services/ scripts/`
Expected: 修掉 I001(import 排序)/F401(未用 import)/F541(无占位 f-string)等 ~12 个;剩 ~18 个 manual。

- [ ] **Step 3: 手动修残余(逐规则,行为不变)**

Run: `ruff check services/ scripts/` 拿残余清单,按规则处理:
- **F841**(未用变量):前缀 `_` 或删赋值。
- **E741**(模糊名 `l`/`I`/`O`):改成描述性名(如 `l` → `line`/`limit`)。
- **E402**(模块级 import 非顶层):多为 conftest「env 先于 import apihub_core」或 lazy import 的合理模式 → 该行加 `# noqa: E402  <原因>`,**不挪动**(挪了破 env-before-import)。
- **B011**(`assert False`):按上下文改 `raise AssertionError(...)` 或 `pytest.fail(...)`。
- **S105**(硬编码密码):dev/test 占位密钥(`apihub_dev_pwd` 类)→ `# noqa: S105  dev/test 占位非生产`。
- **E702**(分号 `;`):拆成多语句/多行。
- **R3**(ruff format 提示):按建议(通常是合并/拆行)。

逐个 file:line 改,改完即 `ruff check <file>` 确认该文件 clean。

- [ ] **Step 4: 全量 ruff clean**

Run: `ruff check services/ scripts/`
Expected: `All checks passed!`

- [ ] **Step 5: 全量回归(逐 suite,确认无行为变更)**

```
.venv/bin/pytest services/libs/apihub-core/tests/ -q
.venv/bin/pytest services/services/auth/tests/ -q
.venv/bin/pytest services/services/dispatcher/tests/ -q
.venv/bin/pytest services/services/notification/tests/ -q
.venv/bin/pytest services/services/quota/tests/ services/services/admin/tests/ services/services/portal/tests/ -q  # 若这些 suite 此前能跑(注意 basename 碰撞 → 逐服务跑)
```
Expected: 无新失败(ruff 改动等价;E741 改名/F841 删变量/B011 改 raise 须不破测试)。

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "r3f T1: ruff 清零（services/ + scripts/ 30→0，等价改写 + 靶向 noqa）"
```

> 注:`git add -A` 会纳入所有 ruff 改动文件;确认 `git status` 无无关文件(如 `.superpowers/` 已 gitignore)。

---

## Task 2: test_db_rls 退到 app role(让 RLS 真生效)

**Files:**
- Modify: `services/libs/apihub-core/tests/test_db_rls.py`
- 依赖:dev PG + seed(`docker compose -f docker-compose.dev.yml up -d postgres` + `make db-apply`)。

**Interfaces:** 无(只改测试)。

- [ ] **Step 1: 起 dev PG + 载 schema/seed**

Run:
```
docker compose -f docker-compose.dev.yml up -d postgres
# 等 ready
until docker compose -f docker-compose.dev.yml exec -T postgres pg_isready -U apihub >/dev/null 2>&1; do sleep 1; done
make db-apply   # 载 scripts/init-db/*.sql（含 01-schema + 02-seed 的 tenant_a/tenant_b）
```
Expected: PG up,02-seed 的 tenant_a/tenant_b 已载入 `api` 表。

确认种子:`docker compose -f docker-compose.dev.yml exec -T postgres psql -U apihub -d apihub -c "SELECT DISTINCT tenant_id FROM api"`(应含 tenant_a/tenant_b)。

- [ ] **Step 2: 复现当前 10 fail(确立 RED)**

Run: `.venv/bin/pytest services/libs/apihub-core/tests/test_db_rls.py -q`
Expected: 不再 skip(PG up),~10 个 RLS 断言 fail(superuser BYPASSRLS → 看到所有租户)。记录失败列表(RED 证据)。

- [ ] **Step 3: 加 `_rls_conn()` helper**

在 `test_db_rls.py` 的 `_connect()` 之后加:
```python
@asynccontextmanager
async def _rls_conn():
    """连 superuser 后退到 app role —— 让 RLS 真生效。

    superuser `apihub` 默认 BYPASSRLS，直接连会看到所有租户 → RLS 断言全 fail。
    `SET LOCAL ROLE apihub_app`（NOSUPERUSER NOBYPASSRLS）在事务内把有效 role 切到
    app role，RLS 强制生效；SET LOCAL 事务级，结束自动还原，无跨测试污染。免 apihub_app 密码。
    """
    async with _connect() as conn, conn.transaction():
        await conn.execute("SET LOCAL ROLE apihub_app")
        yield conn
```

- [ ] **Step 4: 改所有 RLS/enforcement/admin 测试用 `_rls_conn()`**

把 `async with _connect() as conn, conn.transaction():` 改为 `async with _rls_conn() as conn:`
（`_rls_conn` 已含 transaction;去掉外层 `conn.transaction()`），适用于:
- `TestRLSIsolation` 全部(test_tenant_a_cannot_see_tenant_b / test_tenant_b_cannot_see_tenant_a / test_external_tenant_isolation / test_no_tenant_context_sees_nothing)
- `TestPlatformAdminBypass` 全部（保留 `SET LOCAL app.is_platform_admin='true'`——真 admin policy 路径）
- `TestRLSEnforcement` 全部（test_unqualified_select_filters_automatically 等）

> 注意:每个测试体内 `SET LOCAL app.tenant_id = '...'` 等保留不动;只换连接 helper。
> 若某测试确需 superuser owner 权限（本文件无），保留 `_connect()`。

- [ ] **Step 5: 跑 test_db_rls → GREEN**

Run: `.venv/bin/pytest services/libs/apihub-core/tests/test_db_rls.py -q`
Expected: 15 passed(0 skip,0 fail)。RLS 真过滤——tenant_a 只见 tenant_a、admin(is_platform_admin)见全部、无 ctx 见空。

- [ ] **Step 6: 确认 admin 路径在 app role 下仍放行(关键语义)**

人工/断言确认:`test_admin_sees_all_tenants` + `test_admin_can_insert_any_tenant` 在 `_rls_conn()`(app role) + `is_platform_admin='true'` 下 PASS——证明 admin 走的是 **RLS policy 的 admin 分支**（非 superuser bypass），正是该被测的。若 fail,查 RLS policy 是否对 apihub_app 的 is_platform_admin GUC 放行(可能需 `SET LOCAL ROLE apihub_app` 后 GUC 仍生效——`SET LOCAL` GUC 在 SET ROLE 后保留)。

- [ ] **Step 7: 回归 apihub-core suite + ruff**

```
.venv/bin/pytest services/libs/apihub-core/tests/ -q
ruff check services/libs/apihub-core/tests/test_db_rls.py
```
Expected: apihub-core 全绿(含 test_db_rls 15 + 其余);ruff clean。

- [ ] **Step 8: Commit**

```bash
git add services/libs/apihub-core/tests/test_db_rls.py
git commit -m "r3f T2: test_db_rls 退到 app role（SET LOCAL ROLE apihub_app）让 RLS 真生效，15 测绿"
```

- [ ] **Step 9: (可选)关 dev PG**

Run: `docker compose -f docker-compose.dev.yml stop postgres`(保留 volume,下次复用)。

---

## Task 3: 全量回归 + review

- [ ] **Step 1: 全量逐 suite 回归** + `ruff check services/ scripts/`(0)+ `~/.local/bin/mypy`(无新 error,pre-existing 不计)。
- [ ] **Step 2: opus whole-branch review**(per 用户工作风格)。重点:① ruff 改动是否真等价(E741 改名/B011/F841 不破语义);② test_db_rls SET LOCAL ROLE 是否正确退到 app role 且 admin 路径走 policy 非 bypass;③ 无业务代码被改。处理 Critical/Important;handoff 用户 push/merge。

---

## Self-Review

**Spec coverage:** §4.1 ruff(逐规则) → T1 ✓;§4.2 test_db_rls(_rls_conn + SET LOCAL ROLE apihub_app + admin 路径) → T2 ✓;§5 风险(E741/B011 语义、SET ROLE 顺序、种子依赖) → T1 Step5 回归 + T2 Step6 admin 语义确认 ✓。

**Placeholder scan:** T1 残余 ruff 按规则策略处理(执行时 `ruff check` 拿清单逐个改,无法静态枚举 30 个 file:line——已在 Step1/3 说明);余无 TBD。

**Type consistency:** `_rls_conn()` 跨 Step 一致;`_connect()` 保留。
