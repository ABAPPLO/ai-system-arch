# Phase 2 生产化收尾清扫 —— Design Spec

> 日期：2026-07-11 ｜ 分支（拟）：`feat/phase2-hardening-cleanup` ｜ off `main`（`fc16b8e`）
> 术语：「Phase 2 生产化收尾」= 生产准备 + 灰度上线（非 roadmap Phase 3 产品开放）。

## 1. 背景 —— A 段清单已严重过时

`docs/phase2-integration-findings.md` 的「Phase 2 生产化收尾剩余待办」清单是在 2026-07-10
PR #8 之后写的，但其后多轮（PR #11/#12、Phase 3 P1 验证、Argo follow-up）陆续把清单里多项顺手
做掉了，文档没同步。**核对当前代码后，9 项里 6 项已完成**：

| A 段项 | 真实状态 | 代码证据 |
|---|---|---|
| CH 测试数据 INSERT→SELECT（#10） | ✅ 已做 | `scripts/init-clickhouse/01-schema.sql:114-123`（注释 + SELECT 形式） |
| apihub-cli `--dry-run` | ✅ 已做 | `tools/apihub-cli/.../main.py:70-72,82-83,119` |
| test_kafka 4 预存失败 | ✅ 已修 | `services/libs/apihub-core/tests/test_kafka.py:12-17`（fake producer decode bytes） |
| CH Kafka source 列默认值（#9） | ✅ 满足 | `dispatcher/event.py::build_call_event` 全列显式带；`_now_ch_ts` 已 CH 格式 |
| **PG_SSL 默认 disable→prefer** | ❌ 未做 | `apihub_core/config.py:26` 仍 `"disable"` |
| **retry `executor_port=8003` 硬编码** | ❌ 未做 | `retry_svc/main.py:28` 硬编码 |
| prod argo-server 加固 | ⏳ 代码侧已预留 | `config.py:50` `argo_server_insecure`+注释；真 CA/mTLS 属 B 段生产部署 |
| **kind overlay 脚踩坑根治** | ❌ 未根治 | 仅 `bootstrap.sh` canonical，手动单文件 apply 仍会 revert |
| Argo v3.6+ 评估 | 评估项 | 非实现（deferred） |

**本 spec 只做真实剩余的 4 件事**（2 个一行修复 + 文档同步 + kind overlay 根治）。

## 2. 目标 / 范围

### In scope
1. **PG_SSL 默认值** `disable → prefer`。
2. **retry `executor_port` 去硬编码** → 进 `Settings`。
3. **同步 findings doc** 到真实状态（删已完成项、标注真实剩余、deferred 项归类）。
4. **kind overlay 脚踩坑根治**（方案 B：统一 apply 入口 + post-apply 自检 + 文档标注）。

### Out of scope（deferred，写入 findings deferred 段）
- **prod argo-server 加固**（client mode + 真 CA + mTLS）：dev 无法验证，属 B 段生产部署。
  代码侧 `argo_server_insecure` 已预留，本 spec 不动。
- **Argo v3.6+ 升级评估**：评估非实现，待 v3.6 稳定后单独评估。
- `executor_service_template` 的 `{port}` 拼接层重构：已由 overlay env 覆盖解决，本 spec 只去
  `main.py` 的硬编码，不动拼接逻辑（YAGNI）。

## 3. 设计

### 3.1 PG_SSL 默认 `disable → prefer`

**文件**：`services/libs/apihub-core/src/apihub_core/config.py:26`

```python
# asyncpg ssl 值：disable / prefer / require / verify-ca / verify-full
# dev 默认 prefer（先试 SSL，PG 未装 SSL 则回落明文，与 disable 行为一致但面向未来）；
# prod 必须 require 或 verify-full（由 .env 显式覆盖）。
pg_ssl: str = "prefer"
```

**语义**：`prefer` = 有 SSL 用 SSL、无则明文。dev 容器 PG 未装 SSL，行为与 `disable` 等效；prod 仍由
`.env` 显式设 `require/verify-full`，不受默认值影响。

**风险**：极低。纯默认值变更，无调用方改动（`init_pool` 已透传 `settings.pg_ssl`）。

### 3.2 retry `executor_port` 去硬编码

**现状**：`retry_svc/main.py:28` `RetryWorker(executor_port=8003)` 硬编码；`worker.py:42` 默认
也是 8003；`worker.py:46` 用 `EXECUTOR_URL_TEMPLATE.format(port=executor_port)` 拼接
（`config.py:68-70` template 带 `{port}`）。

**改动**（最小，符合 findings 原话「挪进 settings」）：

- `apihub_core/config.py` 加字段（`executor_service_template` 之后）：
  ```python
      # retry worker 调 executor 的端口（k8s 由 EXECUTOR_SERVICE_TEMPLATE 覆盖为无 {port} 时无效；
      # dev 本地回退到带 {port} 的 template 时生效）。默认 8003 = executor 本地端口。
      executor_port: int = 8003
  ```
- `retry_svc/main.py:28`：
  ```python
      worker = RetryWorker(executor_port=settings.executor_port)
  ```
  （`settings = get_settings()` 在 `worker_lifespan` 顶部已有，line 22。）
- `worker.py` 不动（`__init__` 已有 `executor_port` 参数，默认 8003 保留作 fallback）。

**行为不变**：默认仍 8003；新增可经 `EXECUTOR_PORT` env 覆盖。k8s 里 template 被覆盖为无 `{port}`
时 `.format` no-op，executor_port 不影响（仍走 Service:80）。

**风险**：低。不触碰拼接逻辑 / template / configmap。

### 3.3 同步 findings doc

**文件**：`docs/phase2-integration-findings.md` 的「Phase 2 生产化收尾 优先级建议」段（line 225+）。

- 「✅ 已清偿」表保持（PR #8 历史）。
- 「P1」段：三项已验证（traceparent / cross-ns / workflow stub）保持，补注「真 Argo CRD e2e 已于
  PR #11/#12 落地（v3.5.15/emissary）」。
- 「P2」段：删已完成项（CH INSERT #10、dry-run、CH Kafka 列 #9），保留真实剩余
  （`PG_SSL prefer`、`executor_port`、kind overlay 根治）——其中前三项标「本 spec 解决」。
- 「残留小项」：删 test_kafka（已修）、retry executor_port（本 spec 解决）。
- 新增「Deferred」子段：prod argo-server 加固、Argo v3.6+ 评估（含理由）。

**目的**：消除「文档清单 ≠ 代码现状」导致的误判（本 spec §1 即因此而生）。

### 3.4 kind overlay 脚踩坑根治（方案 B）

**根因复述**：`deploy/k8s/base` 与 `deploy/k8s/services/<svc>/*.yaml` 是 kustomize **base**，
overlay（`overlays/kind/patches/*`）patch 了 `ARGO_MODE=k8s`、各服务 `envFrom`（引用
`apihub-shared-infra` cm + `apihub-shared-secret`）、`shared-infra.yaml` 的 `__HOST_IP__`。
用户绕过 `bootstrap.sh` 直接 `kubectl apply -f deploy/k8s/services/workflow/configmap.yaml`
（base 单文件）→ 绕过 kustomize → base 默认值（`ARGO_MODE=stub`、无 envFrom）静默 revert live
资源 → pod crash（如 `pg_user Field required`、workflow stub 模式）。

canonical 路径已在 `bootstrap.sh:136-155`（sed 注入 host IP + `kustomize build --load-restrictor
... overlays/kind | kubectl apply` + `trap git checkout` 还原占位符），但 bootstrap 重建整个集群
（含 kind delete/create、11 镜像 build+load、argo 重装），日常改单个服务重新 apply 不该这么重。

**注意 env 双家族**：
- `overlays/dev|staging|prod` —— 远端 ACK 云环境，Makefile `k8s-apply-*` 用，**无** host IP 注入
  （云上走 in-cluster DNS / 托管 PG）。
- `overlays/kind` —— 本地 kind，`bootstrap.sh` 用，**需** host IP + 端口注入。

#### 3.4.1 统一 apply 入口 `scripts/k8s/apply.sh`

新脚本，唯一 apply 入口，支持两个家族：

```bash
#!/usr/bin/env bash
# 唯一 K8s apply 入口。禁止直接 `kubectl apply -f deploy/k8s/...`（绕过 kustomize 会 revert overlay）。
# 用法：scripts/k8s/apply.sh <env>   env ∈ {kind, dev, staging, prod}
set -euo pipefail

ENV="${1:?usage: apply.sh <kind|dev|staging|prod>}"
case "$ENV" in
  kind)
    OVERLAY=deploy/k8s/overlays/kind
    HOST_IP=$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}')
    SHARED=deploy/k8s/overlays/kind/shared-infra.yaml
    # 注入 host IP + read-back 实际 publish 端口（抽自 bootstrap.sh:126-138）
    PG_HP=$(docker port apihub-pg 5432 2>/dev/null | awk -F: '/^0\.0\.0\.0:/{print $NF;exit}')
    REDIS_HP=$(docker port apihub-redis 6379 2>/dev/null | awk -F: '/^0\.0\.0\.0:/{print $NF;exit}')
    sed -i "s/__HOST_IP__/$HOST_IP/g" "$SHARED"
    sed -i "s/^\(\s*PG_PORT:\s*\"\)5432/\1$PG_HP/" "$SHARED"
    sed -i "s/^\(\s*REDIS_PORT:\s*\"\)6379/\1$REDIS_HP/" "$SHARED"
    trap 'git checkout "$SHARED" 2>/dev/null || true' EXIT
    LOAD_REST="--load-restrictor LoadRestrictionsNone"
    ;;
  dev|staging|prod)
    OVERLAY=deploy/k8s/overlays/$ENV
    LOAD_REST=""
    ;;
  *) echo "unknown env: $ENV" >&2; exit 2 ;;
esac

echo "== kustomize build + apply ($ENV) =="
kustomize build $LOAD_REST "$OVERLAY" | kubectl apply -f -
echo "== apply done. 建议：scripts/k8s/check-overlay.sh $ENV 自检 =="
```

#### 3.4.2 post-apply 自检 `scripts/k8s/check-overlay.sh`

apply 后校验 live 关键字段未被 base 单文件 apply revert；踩坑立即报错而非静默 crash。
**kind 专用**（云环境的期望值不同，本脚本仅覆盖 kind）：

```bash
#!/usr/bin/env bash
# 自检 live 资源的关键 overlay 字段未被 revert（手动 kubectl apply -f base 会 revert）。
# 仅 kind。退出码 0=OK / 1=字段被 revert / 2=env 不支持。
set -euo pipefail
ENV="${1:?usage: check-overlay.sh <kind>}"
[ "$ENV" = "kind" ] || { echo "仅支持 kind（云环境期望值不同）" >&2; exit 2; }
NS=apihub-system
fail=0

# (a) workflow ConfigMap：ARGO_MODE 必须 k8s（base 是 stub）
MODE=$(kubectl -n $NS get cm workflow-config -o jsonpath='{.data.ARGO_MODE}' 2>/dev/null || echo "")
[ "$MODE" = "k8s" ] || { echo "❌ workflow-config ARGO_MODE='$MODE' (期望 k8s) —— 被 base revert？用 apply.sh kind"; fail=1; }

# (b) shared-infra ConfigMap：不得残留 __HOST_IP__ 占位符（host IP 注入漏了）
if kubectl -n $NS get cm apihub-shared-infra -o yaml 2>/dev/null | grep -q '__HOST_IP__'; then
  echo "❌ apihub-shared-infra 残留 __HOST_IP__ —— host IP 注入未生效；用 apply.sh kind"; fail=1
fi

# (c) 每个业务 Deployment 的 envFrom 必须含 apihub-shared-infra（base 无 envFrom）
for d in api-registry dispatcher auth executor quota tenant admin docs trace retry workflow; do
  EF=$(kubectl -n $NS get deploy "$d" -o jsonpath='{.spec.template.spec.containers[0].envFrom}' 2>/dev/null || echo "")
  echo "$EF" | grep -q apihub-shared-infra || { echo "❌ deploy/$d 缺 envFrom apihub-shared-infra —— 被 base revert？用 apply.sh kind"; fail=1; }
done

[ $fail -eq 0 ] && echo "✅ overlay 自检通过（ARGO_MODE / host IP / envFrom 均未被 revert）" || exit 1
```

#### 3.4.3 Makefile + 文档标注

- `Makefile`：`k8s-apply-dev/staging/prod`（line 104-111）改为调 `scripts/k8s/apply.sh <env>`；
  新增 `k8s-apply-kind` target 调 `apply.sh kind`（日常改单服务重 apply，不重建集群）。
  保留 `k8s-apply-*` 名（向后兼容）。
- `deploy/k8s/base/` 与 `deploy/k8s/services/` 顶层各加 `README.md`：明确「这是 kustomize base，
  禁止直接 `kubectl apply -f`；唯一入口 `make k8s-apply-<env>` 或 `scripts/k8s/apply.sh` /
  `scripts/kind/bootstrap.sh`」。
- 关键 base ConfigMap（`workflow/configmap.yaml`）顶部注释加一行警示。

**不做**：不改 base 资源结构（方案 C 排除）；不加 CI 强制门（YAGNI，自检脚本已够）。

## 4. 验证

- **3.1 / 3.2**：`pytest services/libs/apihub-core/tests/ services/services/retry/tests/ -v` 全绿
  （新增/改默认值不应回归）。retry 若无 executor_port 相关单测，加 1 个：`settings.executor_port`
  默认 8003 + env 覆盖生效。
- **3.4**：
  - `scripts/k8s/apply.sh kind` 在线 kind 集群跑通（apply 后 `check-overlay.sh kind` 绿）。
  - **负向验证**：手动 `kubectl apply -f deploy/k8s/services/workflow/configmap.yaml`（模拟踩坑）
    → 跑 `check-overlay.sh kind` → 应报 `ARGO_MODE='stub'` 错（证明自检能抓到 revert）→ 再
    `apply.sh kind` 修复 → `check-overlay.sh` 复绿。
- 全 smoke 回归（`scripts/smoke/k8s-links.py`、`k8s-workflow.py`）仍绿，证明改动无回归。

## 5. 风险 / 回滚

- 所有改动独立、可单独 revert。
- `apply.sh` / `check-overlay.sh` 是新增脚本，不影响既有 `bootstrap.sh`（bootstrap 仍走自己的
  apply 段；后续可让 bootstrap 复用 `apply.sh kind`，但本 spec 不强制，避免牵动 bootstrap 测试面）。
- `pg_ssl` / `executor_port` 默认值变更：行为兼容（prefer≈disable when no SSL；executor_port 默认不变）。

## 6. Self-Review（spec 一致性）

- 范围单一：4 项独立小改，一个 plan 可覆盖。✅
- 无 TBD / 占位符。✅
- 与代码现状一致（每项标了 file:line + 证据）。✅
- kind overlay 双家族（kind vs dev/staging/prod）在 apply.sh 显式分流，check-overlay 仅 kind。✅
