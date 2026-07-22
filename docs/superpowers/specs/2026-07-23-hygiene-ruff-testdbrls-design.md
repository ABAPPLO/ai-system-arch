# Hygiene: pre-existing ruff + test_db_rls RLS bypass

> Date: 2026-07-23 · Round: r3f (hygiene) · Branch: `fix/r3f-hygiene-ruff-testdbrls`
> Status: design (pre-implementation)
> Origin: R2e/R3e CI 一直红（pre-existing Ruff on `scripts/`）；memory 记 test_db_rls「10 RLS fail（superuser BYPASSRLS）」。

## 1. 目标

让 `ruff check services/ scripts/`（CI 的 lint 范围）从 30 errors → **0**，且 `test_db_rls.py` 在 PG up 时从 10 fail → **绿**。**不改变任何运行时行为**——纯 lint/测试修复。

## 2. 非目标

- 不动 `test_identity.py`（隔离跑 15 skip 0 fail；memory 的「collection-error」是 `services/`-together basename 碰撞伪影，非真问题）。
- 不重构、不动业务代码逻辑（ruff 修复只做等价改写或靶向 `noqa`）。
- 不补 `make vendor-check CI`（另立轮）。

## 3. 现状

### 3.1 ruff（`ruff check services/ scripts/` = 30 errors，~17 文件）
按规则：I001(5)、F401(4)、F841(3)、E741(3)、E402(3)、B011(3)、S105(2)、F541(2)、E702(2)、R3(2)。12 个 `--fix` 可自动修。热点文件：`test_ch_tenant_guard.py`(5)、`scripts/smoke/k8s-ai-gateway-dispatcher.py`(4)、`quota/repository.py`(3)。

### 3.2 test_db_rls（PG up 时 10 fail）
`PG_DSN = postgresql://apihub:...@localhost:5432/apihub`——连的是 **superuser `apihub`**。superuser 默认 BYPASSRLS → `SET LOCAL app.tenant_id` 后 `SELECT FROM api` 仍返回**所有租户**行 → `assert tenant_ids == {"tenant_a"}` 等失败。compose 注释（docker-compose.dev.yml:41）确认 `apihub_app` 才是 NOSUPERUSER NOBYPASSRLS 的业务 role。种子数据在 `scripts/init-db/02-seed.sql`（test docstring 已声明依赖）。

## 4. 设计

### 4.1 ruff 修复（逐规则，行为不变）
- **I001 / F401 / F541**（11）：`ruff check --fix` 自动（import 排序 / 删未用 import / 去无占位 f-string）。
- **F841**（3，未用变量）：前缀 `_` 或删除赋值。
- **E741**（3，模糊名 `l`/`I`/`O`）：改成描述性名。
- **E402**（3，模块级 import 非顶层）：多为 conftest「env 先于 import」或 lazy import 的合理模式 → 靶向 `# noqa: E402`（带原因注释），不挪动（挪了会破 env-before-import）。
- **B011**（3，`assert False`）：按上下文改 `raise AssertionError(...)` 或 `pytest.fail(...)`。
- **S105**（2，硬编码密码）：dev/test 里的占位密钥（`apihub_dev_pwd` 类）→ `# noqa: S105`（非生产密钥，有意）。
- **E702**（2，分号）：拆成多语句。
- **R3**（2）：ruff format 提示，按建议。
- **验证**：`ruff check services/ scripts/` → 0 errors；全量 pytest 逐 suite 绿（无行为回归）。

### 4.2 test_db_rls 修复（退到 app role，让 RLS 真生效）
每个测试事务里，连上 superuser `apihub` 后立即 `SET LOCAL ROLE apihub_app`：
- superuser 可 `SET ROLE` 到任意 role；`apihub_app` 为 NOSUPERUSER NOBYPASSRLS → **RLS 在该事务内强制生效**。
- `SET LOCAL` = 事务级，事务结束自动还原，无跨测试污染。
- 无需 `apihub_app` 密码（用现有 superuser 连接 + SET ROLE）。
- admin 测试保留 `SET LOCAL app.is_platform_admin='true'`——这是**真 admin policy 路径**（非 superuser bypass），正是该被测的。
- 落点：抽一个 `_rls_conn()` helper（`_connect()` + `SET LOCAL ROLE apihub_app`），各 RLS/enforcement/admin 测试改用它；`_connect()`（superuser）仅留给确需 owner 权限的（本文件无）。

实现样例：
```python
@asynccontextmanager
async def _rls_conn():
    """连 superuser 后退到 app role —— 让 RLS 真生效（superuser 默认 BYPASSRLS）。"""
    async with _connect() as conn, conn.transaction():
        await conn.execute("SET LOCAL ROLE apihub_app")
        yield conn

# 测试：
async def test_tenant_a_cannot_see_tenant_b(self):
    async with _rls_conn() as conn:
        await conn.execute("SET LOCAL app.tenant_id = 'tenant_a'")
        rows = await conn.fetch("SELECT id, tenant_id FROM api ORDER BY id")
    assert {r["tenant_id"] for r in rows} == {"tenant_a"}
```
- **验证**：`docker compose -f docker-compose.dev.yml up -d postgres` + `make db-apply`（载 01-schema + 02-seed）→ `pytest test_db_rls.py` 绿（15 测全过，不再 skip）。

## 5. 风险

- **ruff 行为变更**：E741 改名 / B011 `assert False`→raise / F841 删变量 须逐个确认不破语义；全量回归兜底。
- **SET LOCAL ROLE 顺序**：须在 `SET LOCAL app.tenant_id` 前先 `SET LOCAL ROLE apihub_app`（role 切换在事务内即可；GUC 设置顺序无依赖）。验证时确认 admin 路径（is_platform_admin GUC）在 app role 下仍放行。
- **种子依赖**：test_db_rls 依赖 02-seed.sql 已载；dev PG 须跑过 `make db-apply`。若 seed 缺 tenant_a/tenant_b → 测试红（非本轮 bug，但验证时确认）。
