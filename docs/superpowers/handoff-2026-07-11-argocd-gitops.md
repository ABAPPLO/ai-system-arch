# Handoff — ArgoCD GitOps（B 段第 1 子项）：全闭环，待开 PR

> B 段（生产部署）第 1 子项 ArgoCD GitOps。**Task 1/2/3 全 done**（drift selfHeal + commit→auto-sync 验证 PASS）。
> 分支 `feat/argocd-gitops` pushed。**待开 PR + 合并 main**。

## 本轮成果（commits on feat/argocd-gitops，off main 06e6c5b）

- **Task 1**（`5c52d97`，review Approved）：`scripts/k8s/argocd-setup.sh` 装 ArgoCD v2.13.2。5 justified 修正（brief 误把 ArgoCD 当 Argo Workflows）：install.yaml URL=raw manifests 非 release asset / grep Not Found→size threshold / `imagePullPolicy: Always→IfNotPresent`（kind air-gapped，否则 ImagePullBackOff）/ application-controller 是 StatefulSet 用 rollout status / crane per-image 重试。Makefile 加 `argocd-setup` target。
- **Task 2**（`4443903` + `e17fd58`）：`deploy/argocd/kind.yaml` Application（path=overlays/kind，repoURL 改 **HTTPS** 因 repo-server 无 SSH agent，selfHeal/prune）+ host IP 修复（`shared-infra.yaml` 用 `host.docker.internal` + 新 `scripts/k8s/patch-coredns-hosts.sh` 运行时解析；CoreDNS 在 kube-system 不归 ArgoCD 管）+ `argocd-setup.sh` 补 `argocd-cm` buildOptions。
- **端口闭环**（`e2398f3`，核心）：`shared-infra`（CM+Secret）**移出 `overlays/kind/kustomization.yaml`** resources + 去 live tracking label 防 prune + `apply.sh` standalone apply + read-back compose 实际端口（15433/16380）patch live CM。实测：ArgoCD sync 后 selfHeal 未 revert/prune，`pg_port=15433`，check-overlay GREEN。
- **config.py 真 bug 修复**（`d62514c`）：PR #13 的 `executor_port: int` 撞 k8s service discovery 自动注入的 `EXECUTOR_PORT=tcp://<clusterIP>:80`（executor Service）→ 所有服务 Settings 崩。`validation_alias="EXECUTOR_APP_PORT"` 修复，field 名不变（非 breaking）。
- **Task 3**（`de1f10c` + auto-sync 验证）：`scripts/smoke/k8s-argocd-gitops.py` drift smoke（scale mock-backend 3→30s selfHeal 回 1，PASS）+ B-c auto-sync（push feat→refresh→ArgoCD fetch rev→sync→live annotation，PASS）。

## 核心 lesson（高价值）

1. **GitOps「git 是真相」vs 运行时值**：host IP（`host.docker.internal` + CoreDNS 运行时解析）+ 动态端口（shared-infra 移出 ArgoCD + apply.sh read-back patch）必须分离到 ArgoCD 管理范围**外**。host.docker.internal 在 kind pod 不解析（pod 不继承 node /etc/hosts）→ CoreDNS hosts patch 解析。
2. **k8s service discovery env 冲突**：k8s 自动注入 `<SVC>_PORT=tcp://...` / `<SVC>_SERVICE_HOST`，pydantic-settings 字段名 `<svc>_port` 会撞 → 用 `validation_alias` 显式 env 名避开。
3. **ArgoCD ServerSideApply 下 `Compare=false` 无效**：controller 经 SSA owns CM fields，照样 revert（实测 6s 内 revert）。要真正脱离 ArgoCD 必须移出 kustomization resources + 去 tracking-id 防 prune。
4. **host proxy 坑**：ArgoCD 镜像 quay.io 经 crane 预载；github fetch 经 HTTPS_PROXY；repo-server 要 HTTPS_PROXY=host.docker.internal:12348 才能 fetch github（host proxy-only）。

## 已知 issue（deferred，下个会话）

1. **auth startup fragility**：deployment 无 `startupProbe` + CoreDNS 启动时序 → rollout restart 时新 pod 偶发 startup 崩（socket.gaierror 连 PG/CH），多次 CrashLoopBackOff 后才 ready。修复：给所有服务 deployment 加 startupProbe（允许 startup ~120s 窗口）。
2. **k8s-links 3/5**（L1/L5 fail）：dispatcher 经 APISIX 调 auth verify 报 "Auth service unreachable"，但 dispatcher 直连 auth `/health/ready:200` —— verify 路径（/v1/apikey/verify）或 APISIX rebuild 后附带；L5 Connection reset（APISIX→dispatcher）。L2/L3/L4 过。
3. **argocd-setup.sh 缺 repo-server HTTPS_PROXY + reconciliation 默认**：fix subagent live patch 了 repo-server HTTPS_PROXY（host proxy-only）+ argocd-cm `timeout.reconciliation=30s`（加速 selfHeal 验证）—— 二者未 commit 进 argocd-setup.sh，fresh install 会缺。
4. **CH 容器 unhealthy**（apihub-clickhouse，但 auth 连 CH 成功 `clickhouse_initialized`，非阻断）。

## 续点（下个会话，按序）

1. **开 PR** `feat/argocd-gitops` → main（Task 1/2/3 + config.py fix d62514c + 端口闭环 e2398f3；squash-merge）。⚠️ merge 前别 re-apply main 的 kind.yaml（main 仍把 shared-infra 放 kustomization → 会重新接管/revert；merge 后 ArgoCD poll main 一致）。
2. 修 auth startupProbe（+ 所有服务 base deployment）。
3. debug k8s-links L1/L5（dispatcher→auth verify / APISIX）。
4. argocd-setup.sh 补 repo-server HTTPS_PROXY + reconciliation 默认值。
5. **B 段后续子项**：告警 rules / Argo Rollouts 灰度 / overlays/prod 补全 / Terraform staging+prod envs。

## 环境（新会话先验证还活着）

- kind `kind-apihub`（活）：12 pods + Argo v3.5.15（argo ns）+ ArgoCD v2.13.2（argocd ns，6 deploy + 1 sts Available）。
- Application `apihub-kind` Synced/Healthy（**live targetRevision=feat/argocd-gitops**；kind.yaml committed=main —— merge 后一致）。
- shared-infra live：`host.docker.internal` + 实际端口（PG 15433 / Redis 16380，apply.sh patch，ArgoCD 不管）。
- 11 服务 + mock-backend healthy（auth 偶发 startup restart 后 ready）。
- host compose 数据层跑（PG/Redis/Kafka/CH/MinIO/Jaeger/OTel），端口 remap（15433/16380/29000）。
