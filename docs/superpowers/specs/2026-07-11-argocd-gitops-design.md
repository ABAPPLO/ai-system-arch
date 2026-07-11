# ArgoCD GitOps（kind 本地验证）—— Design Spec

> 日期：2026-07-11 ｜ 分支：`feat/argocd-gitops` ｜ off `main`（`06e6c5b`）
> B 段（生产部署）第 1 个子项目。决策：**无真实云环境**，做本地 kind 可验证的生产就绪。

## 1. 背景 / 目标

B 段（生产部署）分解为 5 个本地可验证子项，逐个 spec→plan→impl。本 spec 是第 1 个：
**ArgoCD GitOps** —— 在 kind 集群装 ArgoCD，验证「git push → auto-sync → live」的 GitOps 闭环，
作为后续子项（告警 / prod overlay）经 Git 同步的底座。

后续 4 个子项（后续轮次）：告警 rules 补全、Argo Rollouts 灰度、overlays/prod 补全、Terraform
staging/prod envs。

## 2. 范围

### In scope（验证范围 = B 闭环）
1. **装 ArgoCD** 到 kind 的 `argocd` ns（独立 `scripts/k8s/argocd-setup.sh`）。
2. **新增 `deploy/argocd/kind.yaml` Application**（`path: deploy/k8s/overlays/kind`，`selfHeal/prune: true`），本地验证专用。
3. **验证 B 闭环**：
   - (a) Application sync → kind overlay apply → 服务仍 healthy（幂等，不破坏现有 12 pods）。
   - (b) **drift selfHeal**：手改 live 资源（如某 deployment 副本数 / configmap data）→ ArgoCD 自动还原。
   - (c) **commit → auto-sync**：改 kind overlay 某值，push → ArgoCD poll 检测 → 自动 sync → live 更新。

### Out of scope
- **不动 `deploy/argocd/{dev,staging,prod}.yaml`**：它们指云 overlay（`overlays/{dev,staging,prod}`），留待有真实云时。本地只用新增的 `kind.yaml`。
- ArgoCD Webhook（本地 kind 无公网入口，用 poll）。
- argocd CLI 安装（验证用 kubectl 看 Application status jsonpath，不装 CLI）。
- 把告警/prod-overlay 实际经 ArgoCD 同步（后续子项）。
- 真实云部署（Terraform apply / ACK / Harbor）。

## 3. 设计

### 3.1 `scripts/k8s/argocd-setup.sh`（装 ArgoCD）

仿 `scripts/kind/argo-setup.sh` 的成熟模式（release-asset fetch + crane 预载镜像 + apply + 等 ready + 自检）：

```bash
#!/usr/bin/env bash
# 在 kind 装 ArgoCD（GitOps 控制面）。
# 1) fetch 官方 install.yaml（绕 host 代理坑：直连失败回退 HTTPS_PROXY）
# 2) 镜像 host pull（socks5 docker 不支持）→ crane via HTTP 代理预载 → kind load
# 3) kubectl apply + 等 ArgoCD 组件 ready
# 4) 自检
```

- **版本**：`ARGOCD_VERSION="${ARGOCD_VERSION:-v2.13.2}"`（参数化；v2.13 稳定线。plan 时确认 latest stable）。
- **install.yaml URL**：`https://github.com/argoproj/argo-cd/releases/download/${ARGOCD_VERSION}/install.yaml`
  （release asset，版本-pin，含全部 CRD + 组件）。
- **fetch 兜底**：先 `curl --noproxy '*'`，失败回退 `HTTPS_PROXY=http://127.0.0.1:12348`（同 `argo-setup.sh` 模式）。
- **镜像预载**：从 install.yaml 抓 `image:` 字段（`quay.io/argoproj/argocd:${VER}` × 多组件 + redis），
  对每个 `docker image inspect` 探测，缺则 `HTTPS_PROXY=... crane pull <img> /tmp/x.tar && docker load`，
  再 `kind load docker-image`。复用 [[host-proxy-docker-pulls]] 的 crane 模式 + `/tmp/crane`。
- **apply**：`kubectl apply -n argocd -f install.yaml`（install.yaml 自带 Namespace？需确认；若无需显式建 argocd ns，仿 argo-setup.sh）。
- **等 ready**：`kubectl -n argocd wait deploy/argocd-server --for=condition=Available --timeout=300s`。
- **自检**：`kubectl -n argocd get deploy`（server/repo-server/application-controller/redis 都 Available）+ `kubectl get crd applications.argoproj.io`。

### 3.2 `deploy/argocd/kind.yaml`（新增 Application）

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: apihub-kind
  namespace: argocd
spec:
  project: default
  source:
    repoURL: git@github.com:ABAPPLO/ai-system-arch.git
    # 验证阶段临时指向 feat 分支（auto-sync 验证用）；合并后改回 main。
    targetRevision: main
    path: deploy/k8s/overlays/kind
  destination:
    server: https://kubernetes.default.svc
    namespace: apihub-system
  syncPolicy:
    automated:
      prune: true       # kind dev：删 git 里删掉的资源
      selfHeal: true    # kind dev：drift 自动还原（验证 B-b 的关键）
    syncOptions:
      - CreateNamespace=true
      - ServerSideApply=true  # 大型资源避免 apply 拥堵；kind overlay 友好
```

- **path = `overlays/kind`**（非 `overlays/dev`）：本地 kind 跑 host compose 数据层，必须用 kind overlay（host IP 注入）。**这是与现有 dev/staging/prod Application 的关键区别**。
- **repoURL SSH**：仓库 public（已确认 `isPrivate:false`），ArgoCD 默认 known_hosts 含 GitHub → 可匿名 SSH clone，无需配 credential secret。
- **selfHeal/prune = true**：dev/kind 自动同步（同 dev.yaml 语义）。
- **ServerSideApply**：kind overlay 经 `apply.sh` 用 strategic merge；ArgoCD server-side apply 兼容，避免 3-way merge 冲突。plan 时确认是否需要。

### 3.3 验证 B 闭环

**(a) sync + healthy**：
- `kubectl apply -f deploy/argocd/kind.yaml`（或经 argocd-setup.sh 末尾 apply）。
- 等 Application `status.sync.status=Synced` + `status.health.status=Healthy`（`kubectl get application apihub-kind -n argocd -o jsonpath`）。
- 确认 `apihub-system` 12 pods 仍 Running（overlay 幂等 apply，**复用 PR #13 的 `make k8s-check-kind`** 验证 ARGO_MODE/envFrom 未被 revert）。

**(b) drift selfHeal**：
- 手改 live：`kubectl -n apihub-system scale deploy/mock-backend --replicas=3`（overlay 规定 1）。
- 等 ~30s（selfHeal 周期），`kubectl get deploy mock-backend` 副本应自动回 1。
- 断言 Application 仍 `Synced/Healthy`。

**(c) commit → auto-sync**：
- 改 kind overlay 一处无害值（如 `overlays/kind/mock-backend.yaml` 的副本 `replicas: 1 → 2`，或加一个 label）。
- 本轮在 `feat/argocd-gitops` 分支开发，main 还没这 commit。验证策略：**临时把 kind.yaml 的 `targetRevision` 设为 `feat/argocd-gitops`**，push 该分支，ArgoCD poll（默认 3min）拉 feat 分支 → sync → live 副本变 2。验完 targetRevision 改回 `main`（最终交付状态）。
- 加速选项：临时把 argocd-cm 的 `timeout.reconciliation` 调短到 30s 加速验证，验完还原。plan 选其一。

## 4. 约束 / gotchas

- **host proxy 坑**：ArgoCD 镜像（quay.io/argoproj/argocd:* + redis）走 socks5 daemon 拉不到 → crane via HTTP 代理预载。详见 [[host-proxy-docker-pulls]]。
- **fetch install.yaml**：github release CDN 直连 flaky → 直连/HTTPS_PROXY 兜底（同 argo-setup.sh）。
- **kind 资源**：control-plane 6c/64G，ArgoCD ~5 pods（server/repo-server/application-controller/redis/applicationset-controller）够。application-controller 初始可能吃内存，必要时 argocd-cm 调小。
- **poll 不 webhook**：本地 kind 无公网入口，ArgoCD 靠 poll（默认 3min）。验证 auto-sync 要么等 3min，要么临时调短 reconciliation。
- **不破坏现有 kind 状态**：12 pods + Argo v3.5.15 不受影响（ArgoCD 装独立 argocd ns；kind Application 同步 overlay 是幂等 apply）。
- **targetRevision**：最终 kind.yaml `targetRevision: main`；仅 (c) 验证阶段临时指 feat 分支。
- **复用 PR #13 工具**：sync 后跑 `make k8s-check-kind`（check-overlay.sh）确认 overlay 完整。

## 5. 验证

- argocd-setup.sh 自检绿（ArgoCD 组件全 Available + CRD 在）。
- Application `apihub-kind` `Synced + Healthy`。
- `make k8s-check-kind` 绿（overlay 未被 revert）。
- B-b drift：scale mock-backend 3 → 自动回 1。
- B-c auto-sync：改 overlay 副本 → push feat 分支 → ArgoCD poll → live 变更。
- 现有 smoke（`scripts/smoke/k8s-links.py`）仍绿（ArgoCD 装入不破坏链路）。

## 6. 风险 / 回滚

- ArgoCD 装失败/资源不足：`kubectl delete ns argocd`（独立 ns，删了不影响 apihub-system）。CRD `applications.argoproj.io` 留着无害（或手动删）。
- kind Application sync 把 overlay 搞坏：`kubectl delete application apihub-kind -n argocd`，overlay 状态由 `apply.sh kind` canonical 维护。
- selfHeal 误改 live：kind dev 可接受（重新 apply.sh kind 修复）。

## 7. Self-Review

- 范围单一：装 ArgoCD + 1 个 kind Application + B 闭环验证，一个 plan 可覆盖。✅
- 无 TBD：ARGOCD_VERSION 默认值待 plan 确认 latest stable（已标"plan 时确认"，非占位符）。✅
- 与现状一致：dev/staging/prod.yaml 不动（已核）；kind 用 overlays/kind（已核 host IP 注入）。✅
- 复用成熟模式：argo-setup.sh 的 fetch/预载/apply 自检。✅
