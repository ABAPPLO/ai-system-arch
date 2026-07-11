# Phase 2 生产化收尾清扫 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 清掉 Phase 2 生产化收尾的真实剩余技术债：PG_SSL 默认值、retry executor_port 去硬编码、findings 文档同步、kind overlay 脚踩坑根治。

**Architecture:** 4 个独立小改，各自 commit。①②改 `apihub-core` Settings + retry main（单测驱动）；③纯文档同步；④新增 `scripts/k8s/apply.sh`（统一 kustomize apply 入口，分流 kind/dev/staging/prod）+ `check-overlay.sh`（kind post-apply 自检，把"手动 apply base → 静默 revert → pod crash"变成"自检立刻报错"）+ Makefile/README 接线标注。

**Tech Stack:** Python 3.11 / pydantic-settings / pytest（asyncio_mode=auto）/ bash + kustomize + kubectl / kind（context `kind-apihub`）。

## Global Constraints

- **分支**：`feat/phase2-hardening-cleanup`（已建，off `main` `fc16b8e`，spec commit `701f79e`）。
- **kind 集群**：`kind-apihub`（活，12 pods + Argo v3.5.15）。改服务代码须 rebuild 镜像 + `kubectl -n apihub-system rollout restart deploy/<svc>`。
- **Settings 测试模式**：apihub-core 直接 `Settings(pg_host=..., pg_user=..., pg_password=..., redis_host=...)`；env 覆盖用 `monkeypatch.setenv` + `get_settings.cache_clear()`。retry conftest 已注入最小 env（`PG_HOST` 等）+ `clear_settings_cache` autouse fixture。
- **ruff/mypy**：根 `pyproject.toml`（ruff 0.6.9，`.venv-t1/bin/ruff` 兜底）。`make lint`。
- **代理坑**：外网命令加 `env -u HTTPS_PROXY -u HTTP_PROXY -u https_proxy -u http_proxy -u ALL_PROXY -u all_proxy`；curl `--noproxy '*'`。本 plan 的 kustomize/kubectl/docker 都是本地，不涉外网。
- **每 task 末尾 commit**；commit message 用 `feat`/`fix`/`test`/`docs`/`chore` 前缀。
- **不动**：`executor_service_template` 拼接逻辑、base 资源结构、`bootstrap.sh`（新增脚本独立，bootstrap 仍走自己的 apply 段）。

---

## Task 1: Settings —— PG_SSL 默认 prefer + executor_port 字段；retry main 去硬编码

**Files:**
- Create: `services/libs/apihub-core/tests/test_config.py`
- Modify: `services/libs/apihub-core/src/apihub_core/config.py:26`（pg_ssl 默认值）+ `:73` 后（加 executor_port 字段）
- Modify: `services/services/retry/src/retry_svc/main.py:28`

**Interfaces:**
- Consumes: `apihub_core.config.get_settings()`（retry main 已 import，line 11）；retry conftest 的 `fake_settings` fixture（已存在）。
- Produces: `Settings.pg_ssl == "prefer"`（默认）；`Settings.executor_port: int = 8003`（默认）；`retry/main.py` 用 `settings.executor_port` 而非硬编码。

- [ ] **Step 1: 写失败单测 `services/libs/apihub-core/tests/test_config.py`**

```python
"""Settings 默认值 / env 覆盖测试。"""

import pytest

from apihub_core.config import Settings, get_settings

# Settings 必填字段（无默认）：pg_host / pg_user / pg_password / redis_host
_REQUIRED = dict(
    pg_host="localhost",
    pg_user="apihub",
    pg_password="test",  # noqa: S106
    redis_host="localhost",
)


def test_pg_ssl_default_is_prefer():
    """dev 默认 prefer（先试 SSL，无则明文）；prod 由 env 显式覆盖。"""
    s = Settings(**_REQUIRED)
    assert s.pg_ssl == "prefer"


def test_pg_ssl_env_override(monkeypatch):
    monkeypatch.setenv("PG_SSL", "verify-full")
    get_settings.cache_clear()
    assert Settings(**_REQUIRED).pg_ssl == "verify-full"
    get_settings.cache_clear()


def test_executor_port_default():
    s = Settings(**_REQUIRED)
    assert s.executor_port == 8003


def test_executor_port_env_override(monkeypatch):
    monkeypatch.setenv("EXECUTOR_PORT", "9000")
    get_settings.cache_clear()
    assert Settings(**_REQUIRED).executor_port == 9000
    get_settings.cache_clear()
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd /home/applo/project/ai-system-arch
.venv-t1/bin/python -m pytest services/libs/apihub-core/tests/test_config.py -v
```
Expected: 4 FAIL —— `AssertionError: assert 'disable' == 'prefer'`（pg_ssl）；`AttributeError: 'Settings' object has no attribute 'executor_port'`（executor_port 两项）。

- [ ] **Step 3: 改 `config.py` —— pg_ssl 默认值**

`services/libs/apihub-core/src/apihub_core/config.py:24-26`，把：

```python
    # asyncpg ssl 值：disable / prefer / require / verify-ca / verify-full
    # dev 默认 disable（容器内 PG 没装 SSL）；prod 必须 require 或 verify-full
    pg_ssl: str = "disable"
```

改为：

```python
    # asyncpg ssl 值：disable / prefer / require / verify-ca / verify-full
    # dev 默认 prefer（先试 SSL，PG 未装 SSL 则回落明文，与 disable 行为一致但面向未来）；
    # prod 必须 require 或 verify-full（由 .env 显式覆盖）。
    pg_ssl: str = "prefer"
```

- [ ] **Step 4: 改 `config.py` —— 加 executor_port 字段**

在 `config.py` 的 `executor_service_template` 字段之后（约 line 70-73，`workflow_service_url` 之前）插入：

```python
    # retry worker 调 executor 的端口（k8s 由 EXECUTOR_SERVICE_TEMPLATE 覆盖为无 {port} 时无效；
    # dev 本地回退到带 {port} 的 template 时生效）。默认 8003 = executor 本地端口。
    executor_port: int = 8003
```

- [ ] **Step 5: 改 `retry/main.py:28` 去硬编码**

`services/services/retry/src/retry_svc/main.py:28`，把：

```python
    worker = RetryWorker(executor_port=8003)
```

改为：

```python
    worker = RetryWorker(executor_port=settings.executor_port)
```

（`settings = get_settings()` 已在 `worker_lifespan` line 22 取得，直接复用。）

- [ ] **Step 6: 跑测试确认通过 + lint**

```bash
cd /home/applo/project/ai-system-arch
.venv-t1/bin/python -m pytest services/libs/apihub-core/tests/test_config.py -v
.venv-t1/bin/python -m pytest services/services/retry/tests/ -v
.venv-t1/bin/ruff check services/libs/apihub-core/src/apihub_core/config.py \
    services/libs/apihub-core/tests/test_config.py services/services/retry/src/retry_svc/main.py
```
Expected: test_config 4 PASS；retry 全套绿（main.py 改动不回归）；ruff 无错。

- [ ] **Step 7: 可选 live 验证 —— rebuild retry + health/ready**

```bash
cd /home/applo/project/ai-system-arch
docker build -t registry.apihub.internal/apihub/retry:0.1.0-dev \
  -f services/services/retry/Dockerfile . 2>&1 | tail -3
kind load docker-image registry.apihub.internal/apihub/retry:0.1.0-dev --name apihub
kubectl --context kind-apihub -n apihub-system rollout restart deploy/retry
kubectl --context kind-apihub -n apihub-system rollout status deploy/retry --timeout=120s
kubectl --context kind-apihub -n apihub-system port-forward svc/retry 18009:80 &
sleep 3; curl -sf http://127.0.0.1:18009/health/ready && echo " <- retry ready"
kill %1 2>/dev/null || true
```
Expected: retry rollout 成功 + `/health/ready` 200（证明 main.py 改动 live 不崩）。
> 若不便 rebuild（镜像拉取/时间），跳过本步——单测已覆盖绑定逻辑，handoff 注明"live rebuild 待统一轮"。

- [ ] **Step 8: commit**

```bash
cd /home/applo/project/ai-system-arch
git add services/libs/apihub-core/src/apihub_core/config.py \
        services/libs/apihub-core/tests/test_config.py \
        services/services/retry/src/retry_svc/main.py
git commit -m "fix(config): pg_ssl 默认 disable→prefer + retry executor_port 去硬编码进 settings

PG_SSL prefer（dev 与 disable 等效，面向未来）；executor_port 挪进 Settings
（默认 8003，可经 EXECUTOR_PORT 覆盖），retry main 不再硬编码。新增 test_config.py
覆盖默认值 + env 覆盖。"
```

---

## Task 2: findings doc 同步真实状态

**Files:**
- Modify: `docs/phase2-integration-findings.md`（「Phase 2 生产化收尾 优先级建议」段，line 225+）

**Interfaces:** 无代码接口（纯文档）。

- [ ] **Step 1: 读当前 findings 的「Phase 2 生产化收尾」段，定位要改的行**

```bash
cd /home/applo/project/ai-system-arch
grep -n "Phase 2 生产化收尾\|已清偿\|^**P1\|^**P2\|残留小项\|Deferred" docs/phase2-integration-findings.md
```
记下 P2 段、残留小项段的行号。

- [ ] **Step 2: 改 P2 段 —— 删已完成项，标注真实剩余**

把 P2 段（约 line 245-249）改为：

```markdown
**P2（短链路容错）—— 截至 2026-07-11 核对代码**：
- ✅ CH 测试数据 INSERT 改 `INSERT ... SELECT` 形式 —— **已做**（`scripts/init-clickhouse/01-schema.sql:114-123`）
- ✅ `apihub-cli --dry-run` —— **已做**（`tools/apihub-cli/.../main.py:70-72`）
- ✅ ClickHouse Kafka source 列默认值由生产端 JSON 带 —— **已满足**（`dispatcher/event.py::build_call_event` 全列显式带；`_now_ch_ts` 已 CH 格式）
- ✅ `PG_SSL` 默认 `disable→prefer` —— **本 spec 解决**（Task 1）
- ✅ retry `executor_port` 挪进 settings —— **本 spec 解决**（Task 1）
- ✅ kind overlay 脚踩坑根治 —— **本 spec 解决**（Task 3/4：`apply.sh` + `check-overlay.sh`）
```

- [ ] **Step 3: 改「残留小项」段 —— 删已修/本 spec 解决项**

把「残留小项（非阻断…）」段（约 line 251-253）改为：

```markdown
**残留小项**：无（原 retry `executor_port` 硬编码 / `test_kafka` 4 失败均已清，前者本 spec Task 1，后者 PR #8 已修）。
```

- [ ] **Step 4: P1 段补注 Argo CRD e2e 已落地**

P1 段第一行（workflow-svc 端到端联调）的 `→ **已验证（stub）**` 后补一句：

```markdown
（真 Argo CRD e2e 已于 PR #11/#12 落地：v3.5.15/emissary，`K8sArgoClient` 全验证 + MinIO 产物 + resume via argo-server）。
```

- [ ] **Step 5: 新增 Deferred 子段**

在「残留小项」段之后插入：

```markdown
**Deferred（明确不在 Phase 2 生产化收尾）**：
- **prod argo-server 加固**（server auth-mode → client mode + 真 CA + mTLS）：dev 无法验证，属生产部署（roadmap Phase 0/B 段）。代码侧 `Settings.argo_server_insecure` 已预留（`config.py:50`），prod 部署时设 False + argo-server 切 client mode。
- **Argo v3.6+ 升级评估**：v3.5.15 当前稳定，v3.6 待观察后单独评估。
```

- [ ] **Step 6: commit**

```bash
cd /home/applo/project/ai-system-arch
git add docs/phase2-integration-findings.md
git commit -m "docs(findings): 同步 Phase 2 生产化收尾真实状态（6/9 已完成）

P2 段删已完成项（CH INSERT/dry-run/CH Kafka 列），标注真实剩余由本 spec 解决；
P1 补注真 Argo CRD e2e 已落地；新增 Deferred 段（prod argo-server 加固 / Argo v3.6+）。
消除「文档清单 ≠ 代码现状」误判。"
```

---

## Task 3: kind overlay 根治 —— apply.sh 统一入口 + check-overlay.sh 自检

**Files:**
- Create: `scripts/k8s/apply.sh`
- Create: `scripts/k8s/check-overlay.sh`

**Interfaces:**
- Consumes: `deploy/k8s/overlays/{kind,dev,staging,prod}/`（kustomize）；`bootstrap.sh:126-138` 的 host IP + 端口 read-back 逻辑（apply.sh kind 分支复用）。
- Produces: `scripts/k8s/apply.sh <env>`（唯一 apply 入口）；`scripts/k8s/check-overlay.sh kind`（post-apply 自检，退出码 0/1/2）。

- [ ] **Step 1: 写 `scripts/k8s/apply.sh`**

```bash
mkdir -p scripts/k8s
cat > scripts/k8s/apply.sh <<'SCRIPT'
#!/usr/bin/env bash
# =============================================================================
# 唯一 K8s apply 入口。
# 禁止直接 `kubectl apply -f deploy/k8s/...`（绕过 kustomize 会 revert overlay patch，
# 详见 docs/phase2-integration-findings.md「kind overlay 脚踩坑」）。
# 用法：scripts/k8s/apply.sh <env>   env ∈ {kind, dev, staging, prod}
#   kind           本地 kind：注入 host IP + read-back 端口到 shared-infra.yaml，apply 后还原
#   dev/staging/prod 远端 ACK：纯 kustomize build | apply（云上走 in-cluster DNS）
# =============================================================================
set -euo pipefail

ENV="${1:?usage: apply.sh <kind|dev|staging|prod>}"
case "$ENV" in
  kind)
    OVERLAY=deploy/k8s/overlays/kind
    HOST_IP=$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}')
    SHARED=deploy/k8s/overlays/kind/shared-infra.yaml
    # host IP + read-back 实际 publish 端口（抽自 bootstrap.sh:126-138，保证 publish==overlay）
    PG_HP=$(docker port apihub-pg 5432 2>/dev/null | awk -F: '/^0\.0\.0\.0:/{print $NF;exit}')
    REDIS_HP=$(docker port apihub-redis 6379 2>/dev/null | awk -F: '/^0\.0\.0\.0:/{print $NF;exit}')
    [ -n "$PG_HP" ]    || { echo "FATAL: apihub-pg host port read-back empty" >&2; exit 1; }
    [ -n "$REDIS_HP" ] || { echo "FATAL: apihub-redis host port read-back empty" >&2; exit 1; }
    sed -i "s/__HOST_IP__/$HOST_IP/g" "$SHARED"
    sed -i "s/^\(\s*PG_PORT:\s*\"\)5432/\1$PG_HP/" "$SHARED"
    sed -i "s/^\(\s*REDIS_PORT:\s*\"\)6379/\1$REDIS_HP/" "$SHARED"
    trap 'git checkout "$SHARED" 2>/dev/null || true' EXIT
    LOAD_REST="--load-restrictor LoadRestrictionsNone"
    say_host="(host_ip=$HOST_IP pg=$PG_HP redis=$REDIS_HP)"
    ;;
  dev|staging|prod)
    OVERLAY=deploy/k8s/overlays/$ENV
    LOAD_REST=""
    say_host=""
    ;;
  *)
    echo "unknown env: $ENV (expect kind|dev|staging|prod)" >&2
    exit 2
    ;;
esac

echo "== kustomize build + apply ($ENV) $say_host =="
kustomize build $LOAD_REST "$OVERLAY" | kubectl apply -f -
echo "== apply ($ENV) done. 建议跟跑：scripts/k8s/check-overlay.sh $ENV =="
SCRIPT
chmod +x scripts/k8s/apply.sh
```

- [ ] **Step 2: 写 `scripts/k8s/check-overlay.sh`**

```bash
cat > scripts/k8s/check-overlay.sh <<'SCRIPT'
#!/usr/bin/env bash
# =============================================================================
# post-apply 自检：校验 live 资源的关键 overlay 字段未被 revert。
# 手动 `kubectl apply -f <base单文件>` 会绕过 kustomize revert overlay（ARGO_MODE/envFrom/
# __HOST_IP__），导致 pod 静默 crash。本脚本把「静默 crash」变成「立刻报错」。
# 仅 kind（云环境 dev/staging/prod 期望值不同，不在覆盖范围）。
# 退出码：0 OK / 1 有字段被 revert / 2 env 不支持
# =============================================================================
set -euo pipefail

ENV="${1:?usage: check-overlay.sh <kind>}"
[ "$ENV" = "kind" ] || { echo "仅支持 kind（云环境期望值不同）" >&2; exit 2; }

NS=apihub-system
fail=0

# (a) workflow ConfigMap：ARGO_MODE 必须 k8s（base 默认 stub）
MODE=$(kubectl -n "$NS" get cm workflow-config -o jsonpath='{.data.ARGO_MODE}' 2>/dev/null || echo "")
if [ "$MODE" != "k8s" ]; then
  echo "❌ workflow-config ARGO_MODE='$MODE'（期望 k8s）—— 被 base revert？用 scripts/k8s/apply.sh kind"
  fail=1
fi

# (b) shared-infra ConfigMap：不得残留 __HOST_IP__（host IP 注入漏了）
if kubectl -n "$NS" get cm apihub-shared-infra -o yaml 2>/dev/null | grep -q '__HOST_IP__'; then
  echo "❌ apihub-shared-infra 残留 __HOST_IP__ —— host IP 注入未生效；用 scripts/k8s/apply.sh kind"
  fail=1
fi

# (c) 每个业务 Deployment 的 envFrom 必须含 apihub-shared-infra（base 无 envFrom）
for d in api-registry dispatcher auth executor quota tenant admin docs trace retry workflow; do
  EF=$(kubectl -n "$NS" get deploy "$d" -o jsonpath='{.spec.template.spec.containers[0].envFrom}' 2>/dev/null || echo "")
  if ! echo "$EF" | grep -q apihub-shared-infra; then
    echo "❌ deploy/$d 缺 envFrom apihub-shared-infra —— 被 base revert？用 scripts/k8s/apply.sh kind"
    fail=1
  fi
done

if [ "$fail" -eq 0 ]; then
  echo "✅ overlay 自检通过（ARGO_MODE / host IP / envFrom 均未被 revert）"
  exit 0
fi
exit 1
SCRIPT
chmod +x scripts/k8s/check-overlay.sh
```

- [ ] **Step 3: live 正向验证 —— apply.sh kind + check-overlay.sh 绿**

```bash
cd /home/applo/project/ai-system-arch
git diff --stat deploy/k8s/overlays/kind/shared-infra.yaml   # 应为空（干净）
bash scripts/k8s/apply.sh kind 2>&1 | tail -20
bash scripts/k8s/check-overlay.sh kind
echo "exit=$?"
git diff --stat deploy/k8s/overlays/kind/shared-infra.yaml   # apply 后 trap 还原，应仍为空
```
Expected: apply 输出 `kustomize build + apply (kind) ...` + 各资源 configured；check-overlay `✅ overlay 自检通过...`，exit 0；shared-infra.yaml apply 前后都干净（trap 还原）。

- [ ] **Step 4: live 负向验证 —— 模拟踩坑，证明自检能抓到**

```bash
cd /home/applo/project/ai-system-arch
# 模拟用户绕过 kustomize 直接 apply base 单文件（revert ARGO_MODE→stub）
kubectl --context kind-apihub -n apihub-system apply -f deploy/k8s/services/workflow/configmap.yaml
bash scripts/k8s/check-overlay.sh kind
echo "exit=$?  (期望 1 —— 抓到 ARGO_MODE='stub')"
# 修复回正确状态
bash scripts/k8s/apply.sh kind >/dev/null 2>&1
bash scripts/k8s/check-overlay.sh kind
echo "exit=$?  (期望 0 —— 修复后复绿)"
```
Expected: 第一次 check exit 1，报 `❌ workflow-config ARGO_MODE='stub'`；apply.sh kind 修复后 check exit 0。

- [ ] **Step 5: 验证 dev 分支（非 kind）不注入 host IP**

```bash
cd /home/applo/project/ai-system-arch
# dev/staging/prod 不应有 host IP 注入（云环境）。dry-run 确认脚本能 build（不 apply，避免误触云）
kustomize build deploy/k8s/overlays/dev >/dev/null && echo "dev overlay builds OK"
# 确认 apply.sh 对未知 env 报错
bash scripts/k8s/apply.sh bogus 2>&1; echo "exit=$?  (期望 2)"
```
Expected: dev overlay builds OK；`apply.sh bogus` exit 2 + `unknown env`。

- [ ] **Step 6: commit**

```bash
cd /home/applo/project/ai-system-arch
git add scripts/k8s/apply.sh scripts/k8s/check-overlay.sh
git commit -m "feat(k8s): 统一 apply 入口 apply.sh + overlay 自检 check-overlay.sh

apply.sh <kind|dev|staging|prod> 封装 kustomize build|apply（kind 额外注入 host IP +
read-back 端口 + trap 还原），取代散落的直接 kubectl apply。check-overlay.sh kind 自检
live workflow ARGO_MODE / shared-infra __HOST_IP__ / 各 deploy envFrom 未被 base 单文件
apply revert —— 把静默 pod crash 变成立刻报错。负向验证：apply base 单文件→自检抓到。"
```

---

## Task 4: Makefile 接线 + base/services README 标注

**Files:**
- Modify: `Makefile:103-111`（k8s-apply-* 改调 apply.sh + 新增 k8s-apply-kind）
- Create: `deploy/k8s/base/README.md`
- Create: `deploy/k8s/services/README.md`
- Modify: `deploy/k8s/services/workflow/configmap.yaml`（顶部加警示注释）

**Interfaces:**
- Consumes: Task 3 的 `scripts/k8s/apply.sh`。
- Produces: `make k8s-apply-{kind,dev,staging,prod}` 全部经 apply.sh；base/services 顶层 README 警告禁止直接 apply。

- [ ] **Step 1: Makefile k8s-apply-* 改调 apply.sh + 加 k8s-apply-kind**

`Makefile:103-111`，把：

```makefile
# ===== K8s =====
k8s-apply-dev:  ## 同步 dev 环境（本地 kubectl）
	kustomize build deploy/k8s/overlays/dev | kubectl apply -f -

k8s-apply-staging:
	kustomize build deploy/k8s/overlays/staging | kubectl apply -f -

k8s-apply-prod:
	kustomize build deploy/k8s/overlays/prod | kubectl apply -f -
```

改为：

```makefile
# ===== K8s =====
# 所有 apply 经 scripts/k8s/apply.sh（唯一入口；直接 kubectl apply -f base 会 revert overlay）。
k8s-apply-kind:  ## 同步本地 kind 集群（注入 host IP + read-back 端口）
	bash scripts/k8s/apply.sh kind

k8s-apply-dev:  ## 同步 dev 环境（ACK）
	bash scripts/k8s/apply.sh dev

k8s-apply-staging:  ## 同步 staging 环境
	bash scripts/k8s/apply.sh staging

k8s-apply-prod:  ## 同步 prod 环境
	bash scripts/k8s/apply.sh prod

k8s-check-kind:  ## 自检 kind overlay 关键字段未被 revert
	bash scripts/k8s/check-overlay.sh kind
```

- [ ] **Step 2: 写 `deploy/k8s/base/README.md`**

````markdown
# deploy/k8s/base —— kustomize base（⚠️ 禁止直接 kubectl apply）

本目录是 kustomize **base** 资源（namespaces / shared / apigw / argo / db-init）。
各环境差异（host IP、ARGO_MODE、envFrom、端口）由 `deploy/k8s/overlays/<env>/` 的 patch 注入。

**禁止** `kubectl apply -f deploy/k8s/base/...`：会绕过 kustomize，用 base 默认值 revert
live 资源已 patch 的字段 → pod 静默 crash（见 `docs/phase2-integration-findings.md`）。

**唯一 apply 入口**：
- 本地 kind：`make k8s-apply-kind`（或 `scripts/kind/bootstrap.sh` 重建整个集群）
- 云环境：`make k8s-apply-{dev,staging,prod}`
- 底层都走 `scripts/k8s/apply.sh <env>`。

apply 后建议自检：`make k8s-check-kind`（kind）。
````

- [ ] **Step 3: 写 `deploy/k8s/services/README.md`**

````markdown
# deploy/k8s/services —— 各服务 base（⚠️ 禁止直接 kubectl apply）

每个子目录 `<svc>/{configmap,deployment}.yaml` 是该服务的 kustomize **base**（prod 默认形态）。
dev/kind 环境的差异（envFrom 引 apihub-shared-infra、ARGO_MODE=k8s、副本数、host IP）由
`deploy/k8s/overlays/<env>/patches/` 注入。

**禁止** `kubectl apply -f deploy/k8s/services/<svc>/configmap.yaml`：会绕过 kustomize，
revert overlay patch（如把 ARGO_MODE 从 k8s 打回 stub、抹掉 envFrom）→ pod 静默 crash
（`pg_user Field required` / workflow stub 模式）。

**改单个服务后重新 apply 的正确方式**：`make k8s-apply-kind`（或对应 env）→ `make k8s-check-kind`。
若只改了镜像，先 `kind load docker-image` 再 `kubectl rollout restart deploy/<svc>`。
````

- [ ] **Step 4: workflow base configmap 顶部加警示注释**

`deploy/k8s/services/workflow/configmap.yaml` 第 1 行（`apiVersion` 之前）插入：

```yaml
# ⚠️ 这是 kustomize base（ARGO_MODE=stub）。kind/dev 经 overlays/kind/patches/workflow-argo-mode.yaml
# patch 成 k8s。禁止直接 kubectl apply -f 本文件（会 revert overlay → workflow 回 stub 模式）。
# 正确入口：make k8s-apply-kind。详见 deploy/k8s/services/README.md。
```

- [ ] **Step 5: 验证 Makefile target 可用**

```bash
cd /home/applo/project/ai-system-arch
make -n k8s-apply-kind k8s-apply-dev k8s-check-kind 2>&1
```
Expected: 三行 `bash scripts/k8s/apply.sh ...` / `check-overlay.sh kind`（dry-run，不实际执行）。

- [ ] **Step 6: commit**

```bash
cd /home/applo/project/ai-system-arch
git add Makefile deploy/k8s/base/README.md deploy/k8s/services/README.md \
        deploy/k8s/services/workflow/configmap.yaml
git commit -m "chore(k8s): Makefile k8s-apply-* 接线 apply.sh + base/services README 警示标注

k8s-apply-{kind,dev,staging,prod} 全部经 apply.sh（新增 kind）；加 k8s-check-kind。
base/services 顶层 README + workflow base configmap 顶部注释明确禁止直接 kubectl apply -f，
指引唯一入口 make k8s-apply-<env>。"
```

---

## Self-Review（plan vs spec 覆盖）

- spec §3.1 PG_SSL prefer → Task 1 Step 3-4。✅
- spec §3.2 executor_port 去硬编码 → Task 1 Step 4-5。✅
- spec §3.3 findings doc 同步 → Task 2（P2 删已完成 / 残留小项清空 / P1 补 Argo / Deferred 新增）。✅
- spec §3.4.1 apply.sh → Task 3 Step 1。✅
- spec §3.4.2 check-overlay.sh → Task 3 Step 2。✅
- spec §3.4.3 Makefile + README + annotation → Task 4。✅
- spec §4 验证（单测 + live 正向/负向 + smoke 回归）→ Task 1 Step 6-7（单测+可选live）、Task 3 Step 3-5（live 正负向）。smoke 回归未单列 step——Task 3 apply.sh kind 跑通即等价 overlay 正确（smoke 依赖 overlay 正确）；若需显式 smoke，handoff 前补跑 `scripts/smoke/k8s-links.py`。✅（注明）
- 类型一致：`executor_port: int`（Task 1 Step 4 定义）↔ `settings.executor_port`（Step 5 用）↔ `RetryWorker(executor_port=...)`（worker.py:42 既有 int 参数）。✅
- 无占位符：所有 code step 给完整代码；live step 给完整命令 + expected。✅
