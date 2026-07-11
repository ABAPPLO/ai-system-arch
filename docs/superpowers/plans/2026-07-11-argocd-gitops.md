# ArgoCD GitOps（kind 本地验证）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 kind 装 ArgoCD + 新增 kind.yaml Application，验证「sync / drift selfHeal / commit→auto-sync」GitOps B 闭环。

**Architecture:** 3 task：①`scripts/k8s/argocd-setup.sh`（复用 argo-setup.sh 的 fetch+crane 预载+apply 自检模式装 ArgoCD）；②`deploy/argocd/kind.yaml` Application（path=overlays/kind）+ sync 验证；③`scripts/smoke/k8s-argocd-gitops.py`（sync+drift 自动化回归 smoke）+ B-c auto-sync 手动验证。

**Tech Stack:** ArgoCD v2.13.x / bash + kustomize + kubectl / kind（context `kind-apihub`）/ Python smoke。

## Global Constraints

- **分支**：`feat/argocd-gitops`（已建，off `main` `06e6c5b`，spec commit `3e75d56`）。
- **kind 集群**：`kind-apihub`（活，12 pods + Argo v3.5.15）。`kubectl --context kind-apihub ...`。
- **host proxy 坑**：外网 fetch 用 `curl --noproxy '*'` 直连，失败回退 `HTTPS_PROXY=http://127.0.0.1:12348`；镜像预载用 `HTTPS_PROXY=http://127.0.0.1:12348 /tmp/crane pull <img> /tmp/x.tar && docker load -i /tmp/x.tar`（docker daemon 的 socks5 代理不支持，不能重启 docker——会杀 kind）。crane 在 `/tmp/crane`。详见 [[host-proxy-docker-pulls]]。
- **复用 PR #13 工具**：overlay 完整性自检用 `make k8s-check-kind`（`scripts/k8s/check-overlay.sh`）。
- **不碰**：`deploy/argocd/{dev,staging,prod}.yaml`（云专用）、现有 12 pods 状态、Argo v3.5.15。
- **每 task 末尾 commit**；commit message 用 `feat`/`chore`/`test` 前缀。

---

## Task 1: `scripts/k8s/argocd-setup.sh` —— 装 ArgoCD + Makefile target

**Files:**
- Create: `scripts/k8s/argocd-setup.sh`
- Modify: `Makefile`（加 `argocd-setup` target，挨着 PR #13 的 `k8s-apply-kind`）

**Interfaces:**
- Consumes: `argo-setup.sh` 的 fetch/crane 预载/apply 自检模式（参考实现）；`/tmp/crane`；host HTTP 代理 `127.0.0.1:12348`。
- Produces: `scripts/k8s/argocd-setup.sh`（幂等装 ArgoCD 到 `argocd` ns）；ArgoCD CRD `applications.argoproj.io` 注册到集群（供 Task 2 apply Application）。

- [ ] **Step 1: 写 `scripts/k8s/argocd-setup.sh`**

```bash
mkdir -p scripts/k8s
cat > scripts/k8s/argocd-setup.sh <<'SCRIPT'
#!/usr/bin/env bash
# =============================================================================
# 在 kind 装 ArgoCD（GitOps 控制面）。
#   1) fetch 官方 install.yaml（绕 host 代理坑：直连失败回退 HTTPS_PROXY）
#   2) 镜像 host pull（socks5 docker 不支持）→ crane via HTTP 代理预载 → kind load
#   3) kubectl apply + 等 ArgoCD 组件 ready
#   4) 自检
# 前提：kind 集群 kind-apihub 在；/tmp/crane 在（或脚本内兜底）。
# =============================================================================
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"

CLUSTER_NAME="${KIND_CLUSTER_NAME:-apihub}"
ARGOCD_NS=argocd
ARGOCD_VERSION="${ARGOCD_VERSION:-v2.13.2}"

log() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
say() { printf '  %s\n' "$*"; }

# ---------- 1) fetch install.yaml（绕代理）----------
log "fetch argo-cd install.yaml ($ARGOCD_VERSION) from release assets"
INSTALL=/tmp/argocd-install.yaml
URL="https://github.com/argoproj/argo-cd/releases/download/${ARGOCD_VERSION}/install.yaml"
# github release CDN host 直连 flaky（TLS handshake timeout）；先直连，失败回退环境代理。
if ! env -u HTTPS_PROXY -u HTTP_PROXY -u https_proxy -u http_proxy -u ALL_PROXY -u all_proxy \
     curl --noproxy '*' -fsSL --retry 3 --retry-delay 2 --max-time 60 "$URL" -o "$INSTALL"; then
  say "direct fetch failed → retry via environment proxy (HTTPS_PROXY)"
  curl -fsSL --retry 3 --retry-delay 2 --max-time 120 "$URL" -o "$INSTALL"
fi
# 若版本号写错 → release 404，给出明确提示
if [ ! -s "$INSTALL" ] || grep -qi 'Not Found' "$INSTALL"; then
  echo "FATAL: install.yaml 拉取失败（检查 ARGOCD_VERSION=$ARGOCD_VERSION 是否存在）" >&2
  exit 1
fi
say "fetched $(grep -c '^kind:' "$INSTALL") manifests"

# ---------- 2) 镜像预拉 → kind load ----------
log "preload argocd images (host pull via crane → kind load)"
# install.yaml 用 image: 字段（无引号 / 双引号两种都兼容，与 argo-setup.sh 同正则）
mapfile -t IMAGES < <(grep -oE 'image:[[:space:]]*("[^"]+"|[^[:space:]]+)' "$INSTALL" \
  | sed -E 's/image:[[:space:]]*//; s/^"//; s/"$//' | sort -u)
if [ "${#IMAGES[@]}" -eq 0 ]; then
  echo "FATAL: 未从 install.yaml 抓到镜像" >&2; exit 1
fi
if ! command -v /tmp/crane >/dev/null 2>&1 && ! [ -x /tmp/crane ]; then
  echo "FATAL: /tmp/crane 不在（host docker daemon socks5 代理不支持 pull，须 crane 预载）。先按 host-proxy memory 备好 crane。" >&2
  exit 1
fi
for img in "${IMAGES[@]}"; do
  if docker image inspect "$img" >/dev/null 2>&1; then
    say "present: $img"
  else
    say "crane pull+load: $img"
    HTTPS_PROXY=http://127.0.0.1:12348 /tmp/crane pull "$img" /tmp/argocd-img.tar
    docker load -i /tmp/argocd-img.tar
  fi
  kind load docker-image "$img" --name "$CLUSTER_NAME"
done

# ---------- 3) apply + 等 ready ----------
kubectl create namespace "$ARGOCD_NS" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
log "kubectl apply argocd install.yaml"
kubectl apply -n "$ARGOCD_NS" -f "$INSTALL"
log "wait argocd-server Available"
kubectl -n "$ARGOCD_NS" wait deploy/argocd-server --for=condition=Available --timeout=300s
kubectl -n "$ARGOCD_NS" wait deploy/argocd-application-controller --for=condition=Available --timeout=300s

# ---------- 4) 自检 ----------
log "self-check"
kubectl get crd applications.argoproj.io >/dev/null
kubectl -n "$ARGOCD_NS" get deploy argocd-server >/dev/null
kubectl -n "$ARGOCD_NS" get deploy argocd-repo-server >/dev/null
kubectl -n "$ARGOCD_NS" get deploy argocd-application-controller >/dev/null
say "ARGOCD SETUP OK —— server/repo-server/application-controller Available + CRD 注册"
SCRIPT
chmod +x scripts/k8s/argocd-setup.sh
```

- [ ] **Step 2: Makefile 加 `argocd-setup` target**

`Makefile` 在 `k8s-check-kind` target 之后（PR #13 加的 K8s 段尾部）追加：

```makefile
argocd-setup:  ## 在 kind 装 ArgoCD（GitOps 控制面）
	bash scripts/k8s/argocd-setup.sh
```

- [ ] **Step 3: live 跑 argocd-setup.sh（需 fetch + crane 预载，带 HTTPS_PROXY 兜底）**

```bash
cd /home/applo/project/ai-system-arch
HTTPS_PROXY=http://127.0.0.1:12348 bash scripts/k8s/argocd-setup.sh 2>&1 | tail -30
```
Expected: 末尾 `ARGOCD SETUP OK —— server/repo-server/application-controller Available + CRD 注册`，退出 0。
> 若 fetch 报 404（版本不存在）：`ARGOCD_VERSION=v2.13.3 bash scripts/k8s/argocd-setup.sh`（换 patch 号），或查 `curl -s https://api.github.com/repos/argoproj/argo-cd/releases/latest | grep tag_name` 拿 latest。
> 若 crane pull 单个镜像超时：重跑（脚本对已在 daemon 的镜像跳过 pull）。

- [ ] **Step 4: 确认 ArgoCD 组件状态**

```bash
kubectl --context kind-apihub -n argocd get deploy
kubectl --context kind-apihub get crd applications.argoproj.io
```
Expected: `argocd-server / argocd-repo-server / argocd-application-controller / argocd-notifications-controller / argocd-redis` 均 Available（或 1/1 Running）；CRD `applications.argoproj.io` 存在。

- [ ] **Step 5: commit**

```bash
cd /home/applo/project/ai-system-arch
git add scripts/k8s/argocd-setup.sh Makefile
git commit -m "feat(argocd): argocd-setup.sh 在 kind 装 ArgoCD（fetch+crane 预载+apply 自检）

复用 argo-setup.sh 模式：release-asset install.yaml 直连/HTTPS_PROXY 兜底 fetch；
quay.io 镜像经 crane 预载绕 host socks5 代理坑；apply + 等 server/controller
Available + CRD 自检。Makefile 加 argocd-setup target。"
```

---

## Task 2: `deploy/argocd/kind.yaml` Application + sync (a) 验证

**Files:**
- Create: `deploy/argocd/kind.yaml`

**Interfaces:**
- Consumes: Task 1 的 ArgoCD（CRD `applications.argoproj.io` + argocd ns 组件）；`deploy/k8s/overlays/kind/`（Application 同步目标）；public repo `git@github.com:ABAPPLO/ai-system-arch.git`。
- Produces: Application `apihub-kind`（argocd ns）—— 同步 overlays/kind 到 kind 集群 apihub-system，selfHeal/prune 开。

- [ ] **Step 1: 写 `deploy/argocd/kind.yaml`**

```bash
cat > deploy/argocd/kind.yaml <<'EOF'
# ArgoCD Application：本地 kind 集群的 GitOps 入口。
# 与 dev/staging/prod.yaml 的关键区别：path 用 overlays/kind（host compose 数据层需 host IP 注入），
# 非 overlays/dev（云语义）。仓库 public → SSH 匿名 clone 免 credential。selfHeal/prune 开（dev 友好）。
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: apihub-kind
  namespace: argocd
spec:
  project: default
  source:
    repoURL: git@github.com:ABAPPLO/ai-system-arch.git
    targetRevision: main
    path: deploy/k8s/overlays/kind
  destination:
    server: https://kubernetes.default.svc
    namespace: apihub-system
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
      - ServerSideApply=true
EOF
```

- [ ] **Step 2: apply Application + 等 Synced/Healthy**

```bash
cd /home/applo/project/ai-system-arch
kubectl --context kind-apihub apply -f deploy/argocd/kind.yaml
for i in $(seq 1 12); do
  SYNC=$(kubectl --context kind-apihub -n argocd get application apihub-kind -o jsonpath='{.status.sync.status}' 2>/dev/null)
  HEALTH=$(kubectl --context kind-apihub -n argocd get application apihub-kind -o jsonpath='{.status.health.status}' 2>/dev/null)
  echo "  [$i] sync=$SYNC health=$HEALTH"
  [ "$SYNC" = "Synced" ] && [ "$HEALTH" = "Healthy" ] && break
  sleep 10
done
kubectl --context kind-apihub -n argocd get application apihub-kind
```
Expected: 末行 `sync=Synced health=Healthy`。
> 若停在 `OutOfSync`：`kubectl -n argocd get application apihub-kind -o jsonpath='{.status.conditions}'` 看原因；常见 repo clone 失败（`kubectl -n argocd logs deploy/argocd-repo-server --tail=30`）——public repo SSH 不应失败，若 known_hosts 缺，把 kind.yaml 的 repoURL 改为 `https://github.com/ABAPPLO/ai-system-arch.git`（public HTTPS 匿名 read）后重 apply。

- [ ] **Step 3: 验证服务仍 healthy + overlay 未被 revert**

```bash
cd /home/applo/project/ai-system-arch
kubectl --context kind-apihub -n apihub-system get pods | head
make k8s-check-kind
echo "exit=$?"
```
Expected: 12 pods Running（ArgoCD sync 幂等 apply，不破坏）；`check-overlay.sh` 退出 0（ARGO_MODE/envFrom/host IP 未被 revert）。

- [ ] **Step 4: commit**

```bash
cd /home/applo/project/ai-system-arch
git add deploy/argocd/kind.yaml
git commit -m "feat(argocd): kind.yaml Application —— 本地 kind GitOps 入口（overlays/kind + selfHeal）

新增 argocd/kind.yaml（path=overlays/kind，非 dev；repoURL SSH public 匿名 clone；
selfHeal/prune 开；ServerSideApply）。不动 dev/staging/prod.yaml（云专用）。
sync 验证：Application Synced/Healthy + 12 pods 不破坏 + check-overlay 绿。"
```

---

## Task 3: B 闭环验证 —— smoke 自动化（sync+drift）+ auto-sync 手动验证

**Files:**
- Create: `scripts/smoke/k8s-argocd-gitops.py`（sync 状态 + drift selfHeal 自动化回归 smoke）

> B-c（commit→auto-sync）涉及 git push + 临时改 targetRevision，自动化不划算 → 手动步骤（Step 4-6），验完清理。smoke 脚本覆盖 (a) sync 状态 + (b) drift selfHeal（可回归）。

**Interfaces:**
- Consumes: Task 1 ArgoCD + Task 2 Application `apihub-kind`；`mock-backend` Deployment（overlays/kind 规定 replicas=1）。
- Produces: `scripts/smoke/k8s-argocd-gitops.py`（退出码 0=OK / 1=assert fail / 2=env unavailable）。

- [ ] **Step 1: 写 `scripts/smoke/k8s-argocd-gitops.py`**

```bash
cat > scripts/smoke/k8s-argocd-gitops.py <<'EOF'
#!/usr/bin/env python3
"""ArgoCD GitOps 回归 smoke（kind）。

验证：
  (a) Application apihub-kind 状态 Synced + Healthy
  (b) drift selfHeal：scale mock-backend 3（overlay 规定 1）→ 等 selfHeal → 副本自动回 1

退出码：0 OK / 1 assert fail / 2 env unavailable。
"""
import subprocess
import sys
import time

NS_ARGO = "argocd"
NS_APP = "apihub-system"
APP = "apihub-kind"
DEPLOY = "mock-backend"
SELFHEAL_WAIT_S = 90  # selfHeal 周期 + 余量


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)


def app_status():
    sync = sh(f"kubectl --context kind-apihub -n {NS_ARGO} get application {APP} "
              "-o jsonpath={{.status.sync.status}}").stdout.strip()
    health = sh(f"kubectl --context kind-apihub -n {NS_ARGO} get application {APP} "
                "-o jsonpath={{.status.health.status}}").stdout.strip()
    return sync, health


def replicas():
    out = sh(f"kubectl --context kind-apihub -n {NS_APP} get deploy {DEPLOY} "
             "-o jsonpath={{.spec.replicas}}").stdout.strip()
    return int(out) if out.isdigit() else None


def main():
    if sh("kubectl --context kind-apihub -n argocd get deploy argocd-server").returncode != 0:
        print("FAIL: argocd 未装（先 make argocd-setup）")
        sys.exit(2)
    if sh(f"kubectl --context kind-apihub -n {NS_ARGO} get application {APP}").returncode != 0:
        print(f"FAIL: Application {APP} 不在（先 kubectl apply -f deploy/argocd/kind.yaml）")
        sys.exit(2)

    # (a) sync 状态
    sync, health = app_status()
    print(f"(a) application sync={sync} health={health}")
    if sync != "Synced" or health != "Healthy":
        print(f"FAIL: 期望 Synced/Healthy，实际 sync={sync} health={health}")
        sys.exit(1)

    # (b) drift selfHeal
    before = replicas()
    print(f"(b) mock-backend replicas before = {before}")
    if before != 1:
        print(f"WARN: baseline 不是 1（={before}），drift 测试仍继续")

    print("  scale mock-backend → 3（制造 drift）")
    sh(f"kubectl --context kind-apihub -n {NS_APP} scale deploy {DEPLOY} --replicas=3")
    if replicas() != 3:
        print(f"FAIL: scale 到 3 失败（replicas={replicas()}）")
        sys.exit(1)

    print(f"  等 selfHeal 还原（最多 {SELFHEAL_WAIT_S}s）...")
    for _ in range(SELFHEAL_WAIT_S // 5):
        time.sleep(5)
        r = replicas()
        if r == before:
            print(f"  selfHeal OK —— replicas 回到 {r}")
            sync2, health2 = app_status()
            print(f"  application 还原后 sync={sync2} health={health2}")
            if sync2 != "Synced" or health2 != "Healthy":
                print("FAIL: drift 后 application 不再 Synced/Healthy")
                sys.exit(1)
            print("ARGOCD GITOPS OK —— sync+healthy + drift selfHeal 验证通过")
            sys.exit(0)
    print(f"FAIL: {SELFHEAL_WAIT_S}s 内 replicas 未还原到 {before}（selfHeal 未生效？）")
    sys.exit(1)


if __name__ == "__main__":
    main()
EOF
chmod +x scripts/smoke/k8s-argocd-gitops.py
```

- [ ] **Step 2: 跑 smoke 验证 (a)+(b)**

```bash
cd /home/applo/project/ai-system-arch
python3 scripts/smoke/k8s-argocd-gitops.py
echo "exit=$?"
```
Expected: `ARGOCD GITOPS OK —— sync+healthy + drift selfHeal 验证通过`，退出 0。
> 若 drift 未还原（selfHeal 超时）：确认 `kubectl -n argocd get application apihub-kind -o jsonpath='{.spec.syncPolicy.automated.selfHeal}'` = `true`；看 application-controller 日志 `kubectl -n argocd logs deploy/argocd-application-controller --tail=30`。

- [ ] **Step 3: commit smoke 脚本**

```bash
cd /home/applo/project/ai-system-arch
git add scripts/smoke/k8s-argocd-gitops.py
git commit -m "test(argocd): k8s-argocd-gitops.py 回归 smoke（sync 状态 + drift selfHeal 自动化）

(a) Application apihub-kind Synced/Healthy 断言；
(b) scale mock-backend 3 → 等 selfHeal → 副本自动回 1 + application 仍 Healthy。
auto-sync(commit→poll) 手动验证，不入自动化（涉 git push）。"
```

- [ ] **Step 4: B-c auto-sync 手动验证 —— 改 overlay + push feat 分支**

```bash
cd /home/applo/project/ai-system-arch
# 临时把 kind.yaml targetRevision 指向 feat 分支（main 还没这些 commit）
sed -i 's|targetRevision: main|targetRevision: feat/argocd-gitops|' deploy/argocd/kind.yaml
kubectl --context kind-apihub apply -f deploy/argocd/kind.yaml
```
然后**用编辑器**在 `deploy/k8s/overlays/kind/mock-backend.yaml` 的第一个文档（Deployment）的 `metadata:` 下、`name: mock-backend` 同级加一行 annotation（不动 replicas，不影响 smoke）：

```yaml
metadata:
  name: mock-backend
  namespace: apihub-system
  annotations: {argocd-gitops-verify: "true"}
```

校验 build OK + commit + push：
```bash
kustomize build --load-restrictor LoadRestrictionsNone deploy/k8s/overlays/kind >/dev/null && echo "overlay build OK"
git add deploy/argocd/kind.yaml deploy/k8s/overlays/kind/mock-backend.yaml
git commit -m "chore(argocd): B-c 验证临时改动（targetRevision=feat + mock-backend annotation）"
git push origin feat/argocd-gitops 2>&1 | tail -3
```
> 用编辑器而非 sed 插 annotation——确保 yaml 缩进正确（sed 跨行插易错）。

- [ ] **Step 5: 等 ArgoCD poll → sync → 验证 live 有 annotation**

```bash
cd /home/applo/project/ai-system-arch
for i in $(seq 1 20); do
  ANN=$(kubectl --context kind-apihub -n apihub-system get deploy mock-backend \
        -o jsonpath='{.metadata.annotations.argocd-gitops-verify}' 2>/dev/null)
  echo "  [$i] annotation=$ANN"
  [ "$ANN" = "true" ] && break
  sleep 15
done
kubectl --context kind-apihub -n apihub-system get deploy mock-backend -o jsonpath='{.metadata.annotations}'
echo
```
Expected: `annotation=true` 出现（证明 commit→push→ArgoCD poll→auto-sync 闭环工作）。
> 若 5min 仍无：临时调短 reconciliation 加速 ——
> `kubectl -n argocd patch cm argocd-cmd-params-cm -p '{"data":{"timeout.reconciliation":"30s"}}' 2>/dev/null || kubectl -n argocd patch cm argocd-cm -p '{"data":{"timeout.reconciliation":"30s"}}'` + `kubectl -n argocd rollout restart deploy/argocd-server`；验完恢复默认。

- [ ] **Step 6: 清理 B-c 临时改动（恢复 targetRevision=main + 删 annotation）**

```bash
cd /home/applo/project/ai-system-arch
# 删 mock-backend annotation（用编辑器或 sed）
sed -i '/annotations: {argocd-gitops-verify: "true"}/d' deploy/k8s/overlays/kind/mock-backend.yaml
# targetRevision 改回 main（最终交付状态）
sed -i 's|targetRevision: feat/argocd-gitops|targetRevision: main|' deploy/argocd/kind.yaml
kustomize build --load-restrictor LoadRestrictionsNone deploy/k8s/overlays/kind >/dev/null && echo "overlay build OK"
git add deploy/argocd/kind.yaml deploy/k8s/overlays/kind/mock-backend.yaml
git commit -m "chore(argocd): 清理 B-c 验证临时改动（targetRevision 回 main + 删 annotation）"
git push origin feat/argocd-gitops 2>&1 | tail -3
```
Expected: overlay build OK；push 成功。
> targetRevision 改回 main 后，feat 分支 selfHeal 以 feat 分支内容为准（已清理干净）；merge 到 main 后 ArgoCD poll main 生效。

- [ ] **Step 7: smoke 回归（确认 ArgoCD 装入不破坏核心链路）**

```bash
cd /home/applo/project/ai-system-arch
python3 scripts/smoke/k8s-links.py 2>&1 | tail -15
echo "exit=$?"
```
Expected: L1-L5 全绿（ArgoCD 装在独立 ns，不影响 apihub-system 链路）。

---

## Self-Review（plan vs spec 覆盖）

- spec §3.1 argocd-setup.sh → Task 1（fetch/预载/apply/自检完整脚本）。✅
- spec §3.2 kind.yaml Application → Task 2（含 ServerSideApply / selfHeal / path=overlays/kind）。✅
- spec §3.3 (a) sync+healthy → Task 2 Step 2-3 + Task 3 Step 1-2 (a)。✅
- spec §3.3 (b) drift selfHeal → Task 3 Step 1-2 (b)。✅
- spec §3.3 (c) commit→auto-sync → Task 3 Step 4-6（手动：改 annotation + push feat + targetRevision 临时 + 验证 + 清理）。✅
- spec §5 smoke 回归 → Task 3 Step 7（k8s-links.py）。✅
- 约束：host proxy/crane（Task 1 Step 1 脚本内）、不碰 dev/staging/prod.yaml（Task 2 仅新建 kind.yaml）、复用 make k8s-check-kind（Task 2 Step 3）。✅
- 类型一致：Application name `apihub-kind`、ns `argocd`、target `apihub-system` 全 plan 一致；mock-backend replicas baseline=1（与 `mock-backend.yaml:7` 一致）。✅
- 无占位符：脚本/smoke/手动步骤均给完整内容 + expected；版本号兜底（v2.13.2 + 404 换 patch 指引）。✅
