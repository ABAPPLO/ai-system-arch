# Handoff — ArgoCD GitOps（B 段第 1 子项）：host IP 修复 done，端口 deferred

> 跨会话交接。B 段（生产部署）第 1 子项「ArgoCD GitOps」。本轮：Task 1（装 ArgoCD）done、Task 2（kind.yaml
> Application）部分 done（host IP 冲突已修，**端口冲突 deferred**）、Task 3（B 闭环 smoke + auto-sync）未开始。
> 分支 `feat/argocd-gitops`，已 push（off `main` `06e6c5b`）。

## 本轮成果

- **Task 1**（`8280f2b..5c52d97`，review Approved）：`scripts/k8s/argocd-setup.sh` 在 kind 装 ArgoCD v2.13.2
  （6 deploy + 1 sts Available + 3 CRD）。5 个 justified 修正（brief 误把 ArgoCD 当 Argo Workflows）：
  install.yaml URL 是 raw manifests 非 release asset / grep Not Found→size threshold / imagePullPolicy
  Always→IfNotPresent（kind air-gapped，否则 ImagePullBackOff）/ application-controller 是 StatefulSet 用
  rollout status / crane per-image 重试。Makefile 加 `argocd-setup` target。
- **Task 2**（`5c52d97..4443903` + fix `e17fd58`）：`deploy/argocd/kind.yaml` Application（path=overlays/kind，
  selfHeal/prune，repoURL 改 **HTTPS** —— repo-server 无 SSH agent）。
- **host IP 修复**（`e17fd58`，核心进展）：发现 `apply.sh` 的 `__HOST_IP__` 运行时注入与 GitOps「git 是真相」冲突
  （ArgoCD 同步 git 原文把字面 `__HOST_IP__` 推 live → pod 重启崩）。正解：**分离 git 真相 / 运行时注入** ——
  `shared-infra.yaml` 用 `host.docker.internal`（git 有效）+ 新 `scripts/k8s/patch-coredns-hosts.sh`（运行时
  patch CoreDNS hosts 解析 `host.docker.internal→docker 网桥 IP`，CoreDNS 在 kube-system 不归 ArgoCD 管，
  selfHeal 不碰）。`apply.sh`/`bootstrap.sh` 去 `__HOST_IP__` sed，改调 CoreDNS patcher。`argocd-setup.sh`
  补 `argocd-cm` 的 `kustomize.buildOptions=--load-restrictor LoadRestrictionsNone`（否则 ArgoCD build
  overlay 报 security 错）。实测：CoreDNS hosts 后 pod 解析 `host.docker.internal`→172.17.0.1；
  ArgoCD-synced api-registry reload healthy；`make k8s-check-kind` GREEN；`k8s-links.py` L1-L5 GREEN。

## ⚠️ 下个会话首要 —— 端口冲突（deferred，同类于 host IP 但无稳定名）

**根因**：host 上 5432/6379/9000 被占 → compose remap（PG→15433 / Redis→16380 / MinIO→29000）。这些端口在
ArgoCD 管的 `shared-infra` CM 里（`5432`/`6379` 等标准值）→ ArgoCD selfHeal 会把运行时实际端口（15433/16380）
revert 回标准值 → 真服务（api-registry 等连 PG 的）重启读 5432 连不上 host:15433 → 崩。

**占用进程**（已探测，均 localhost 绑定但足以让 compose `0.0.0.0:port` 冲突 remap）：
- `5432` = localhost **postgresql**（`127.0.0.1:5432`）
- `6379` = **redis-server**（system redis，`127.0.0.1:6379` + `[::1]:6379`）
- `9000` = 被占（ss 未抓到 pid，`sudo lsof -i:9000` 排查，疑 lobe-network）

**用户已决策：释放 host 标准端口**（停占用服务，让 compose 用标准映射 → shared-infra 原文即有效 → ArgoCD 不再 revert）。

### 释放后重验步骤（用户释放端口后执行）
```bash
# 1) 用户先停占用（用户的环境操作，不代停）：
#    sudo systemctl stop postgresql   # 或 docker stop <pg容器>
#    sudo systemctl stop redis         # 或 redis-cli shutdown
#    sudo lsof -i:9000 → 停掉
# 2) compose 重建用标准端口
make dev-down && make dev-up          # 或 docker compose -f docker-compose.dev.yml down/up
docker port apihub-pg 5432            # 应显示 0.0.0.0:5432（不再 15433）
# 3) 重 apply + 触发 ArgoCD sync
bash scripts/k8s/apply.sh kind        # shared-infra 标准端口 + CoreDNS patch
kubectl --context kind-apihub -n apihub-system rollout restart deploy/api-registry  # 真服务重启 = crash test
kubectl --context kind-apihub -n apihub-system rollout status deploy/api-registry --timeout=120s
# 4) 验证闭环
make k8s-check-kind                   # GREEN（无 __HOST_IP__，端口标准）
kubectl --context kind-apihub -n apihub-system port-forward svc/api-registry 18000:80 &
curl -sf http://127.0.0.1:18000/health/ready   # 200
# 5) 端口闭环达成 → 进 Task 3
```
> shared-infra.yaml 在 `e17fd58` 已是标准端口原文（5432/6379/8123/9000），无需再改代码，只需 host 释放 + compose 标准映射。

## Task 3 续点（plan 已就绪，端口闭环后执行）

`docs/superpowers/plans/2026-07-11-argocd-gitops.md` Task 3：
- **Step 1-2**：`scripts/smoke/k8s-argocd-gitops.py`（sync 状态 + drift selfHeal 自动化：scale mock-backend 3→等 selfHeal→回 1）。
- **Step 3**：commit smoke。
- **Step 4-6**：B-c auto-sync 手动验证（mock-backend 加 annotation + 临时 kind.yaml `targetRevision: feat/argocd-gitops` + push + 等 ArgoCD poll→sync→live 有 annotation → 清理：删 annotation + targetRevision 改回 main）。
- **Step 7**：`k8s-links.py` 回归。
> Task 3 全用 mock-backend（不连 PG），**不受端口冲突影响**——理论上端口未解决也能跑 Task 3，但为闭环完整性建议先端口闭环。

## 关键约束 / gotchas（承接）

- **分支**：`feat/argocd-gitops`（已 push）。spec `3e75d56` / plan `8280f2b`。
- **host proxy 坑**：镜像经 crane（`/tmp/crane`，`HTTPS_PROXY=http://127.0.0.1:12348`）；不重启 docker。详见 memory [[host-proxy-docker-pulls]]。
- **ArgoCD GitOps 在 kind 的根本限制**：host IP + 动态端口是运行时值，必须有 ArgoCD 管理范围外的注入点（CoreDNS patch / 释放标准端口）。host IP 已用 host.docker.internal + CoreDNS 解决；端口待释放标准端口。
- **kind.yaml repoURL = HTTPS**（非 SSH，repo-server 无 SSH agent）。
- **argocd-setup.sh 设了 argocd-cm buildOptions**（durable，fresh install 可用）。
- **不碰** `deploy/argocd/{dev,staging,prod}.yaml`（云专用）。

## 环境（新会话先验证还活着）

- kind `kind-apihub`（活，12 pods + Argo v3.5.15 + ArgoCD v2.13.2 in `argocd` ns）。
- Application `apihub-kind` Synced/Healthy（foreground sync 工作；后台 git ls-refs 偶超时因 host proxy 从 pod 不可达，foreground/selfHeal 恢复）。
- host compose 数据层跑（PG/Redis/Kafka/CH/MinIO/Jaeger/OTel），端口 remap 中（15433/16380/29000）。
- 工作树：feat/argocd-gitops pushed；`.venv-t1/` untracked（本地 venv）。
