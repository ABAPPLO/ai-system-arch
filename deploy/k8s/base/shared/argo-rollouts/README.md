# Argo Rollouts · Canary 灰度发布

> APIHub 服务灰度发布基础设施（详见 [docs/05-core-flows.md §8](../../../../docs/05-core-flows.md)）。

## 前置条件

集群需要安装 Argo Rollouts controller：

```bash
kubectl create namespace argo-rollouts
kubectl apply -n argo-rollouts -f \
  https://github.com/argoproj/argo-rollouts/releases/latest/download/install.yaml

# 本地装 CLI（可选，强烈推荐）
brew tap argoproj/tap && brew install argoproj/tap/kubectl-argo-rollouts
# 或下载二进制：
# https://github.com/argoproj/argo-rollouts/releases/latest/download/kubectl-argo-rollouts-linux-amd64
```

## 当前覆盖

| 服务 | dev | staging | prod | 备注 |
|------|-----|---------|------|------|
| dispatcher | Deployment | **Rollout** ✅ | **Rollout** ✅ | 试点；最高 QPS，灰度价值最大 |
| api-registry | Deployment | Deployment | Deployment | 内部用，不灰度（评审工单约束变更） |
| executor / retry / auth / quota / tenant / admin / docs / trace / workflow | Deployment | Deployment | Deployment | 待二期推广 |

后续可按需将更多服务从 Deployment 切到 Rollout（复制 `services/dispatcher/rollout.yaml` 改名即可）。

## 灰度流程

```
新版本镜像 push → kubectl argo rollouts set image dispatcher app=...:v2
                                       │
                                       ▼
                       Rollout 起 ReplicaSet v2，0% 流量
                                       │
                            ┌──────────┴──────────┐
                            │  step 1: setWeight 5% │
                            │  pause 5min           │
                            │  analysis（5xx + P99）│ ── 失败 ──→ abort，回 stable
                            └──────────┬──────────┘
                                       │
                            ┌──────────┴──────────┐
                            │  step 2: setWeight 25% │
                            │  pause 5min            │
                            │  analysis              │ ── 失败 ──→ abort
                            └──────────┬──────────┘
                                       │
                            ┌──────────┴──────────┐
                            │  step 3: setWeight 50% │
                            │  pause 10min           │
                            │  analysis（5xx + 业务成功率）│ ── 失败 ──→ abort
                            └──────────┬──────────┘
                                       │
                            ┌──────────┴──────────┐
                            │  step 4: setWeight 100% │
                            │  自动 promote           │
                            └──────────────────────┘
```

**判据（AnalysisTemplate）**：
- `api-error-rate` —— stable 与 canary 的 5xx 错误率都 < 1%
- `api-latency-p99` —— canary P99 < 1s
- `business-success-rate` —— canary 业务成功率 ≥ 99%

## 常用命令

### 发布新版本

```bash
# 触发灰度（改镜像即可，Rollout 会按 steps 推进）
kubectl argo rollouts set image dispatcher \
  app=registry.apihub.internal/apihub/dispatcher:0.2.0 \
  -n apihub-system

# 看 dashboard（终端 UI）
kubectl argo rollouts get rollout dispatcher -n apihub-system --watch
```

### 手动推进 / 暂停 / 中断

```bash
# 推进到下一步（跳过当前 pause）
kubectl argo rollouts promote dispatcher -n apihub-system

# 跳过所有 pause 直接全量（慎用，绕过观察）
kubectl argo rollouts promote dispatcher -n apihub-system --full

# 主动暂停（发现问题但还想观察一会儿）
kubectl argo rollouts pause dispatcher -n apihub-system

# 中断并立即回滚到 stable
kubectl argo rollouts abort dispatcher -n apihub-system
```

### 回滚到旧版本

```bash
# 看历史版本
kubectl argo rollouts history dispatcher -n apihub-system

# 回到上一个版本
kubectl argo rollouts undo dispatcher -n apihub-system

# 回到指定 revision
kubectl argo rollouts undo dispatcher -n apihub-system --to-revision=3
```

### 看实时状态

```bash
# 终端 UI
kubectl argo rollouts get rollout dispatcher -n apihub-system --watch

# 看当前 ReplicaSet 比例
kubectl get rollout dispatcher -n apihub-system -o yaml | grep -A 10 status

# 看 analysis 结果（每个 step 会起一个 AnalysisRun）
kubectl get analysisrun -n apihub-system
```

## 文件结构

```
deploy/k8s/
├── base/shared/argo-rollouts/
│   ├── analysis-templates.yaml   # 共享 AnalysisTemplate（所有 Rollout 引用）
│   └── README.md                 # 本文件
└── services/dispatcher/
    ├── configmap.yaml
    ├── deployment.yaml           # dev 用（保留，单副本 RollingUpdate）
    └── rollout.yaml              # staging/prod 用（Rollout + stable/canary 双 Service）
```

## 流量切分原理

dispatcher 用 **host-weight 模式**（Argo Rollouts 默认）：

- 5 副本时：5% canary ≈ 1 canary pod + 4 stable pod
- stable Service（`dispatcher`）和 canary Service（`dispatcher-canary`）通过 selector 识别各自 pods
- 调用方一直用 `dispatcher` DNS（保持兼容）；Argo 自动 patch 这个 Service 的 selector，按权重切流量

**APISIX upstream 联动**（二期可加）：

当前 host-weight 是 K8s Service 层 round-robin，精度有限。要实现更精细的流量切分（按 Header / 调用方 / 地区），可以加 APISIX 的 `traffic-split` 插件 + Argo Rollouts 的 `TrafficRouting`：

```yaml
strategy:
  canary:
    trafficRouting:
      plugins:
        argoproj-labs/apisix-traffic-router:
          rules:
            - canaryService: dispatcher-canary
              stableService: dispatcher
```

详见 [Argo Rollouts Traffic Management](https://argo-rollouts.readthedocs.io/en/stable/features/traffic-management/).

## 失败回滚保障

1. **AnalysisTemplate 自动 abort**：任一 analysis step 失败 → Rollout 立即 abort，新 RS 副本归 0，流量全部回 stable
2. **maxSurge: 1, maxUnavailable: 0**：升级过程中至少保持 `replicas - 0` 个可用 Pod
3. **Rollout 历史 revision**：每次镜像变更生成新 revision，可 `undo` 回任意历史版本
4. **APISIX 健康检查**：即使 Rollout 没切流量，APISIX upstream 也对 dispatcher 做主动健康检查，Pod 不健康自动摘除

## 添加新服务（推广 Rollout 到其它服务）

1. 复制 `services/dispatcher/rollout.yaml` → `services/<svc>/rollout.yaml`
2. 全局替换 `dispatcher` → `<svc>`
3. 调整 `strategy.canary.steps` 的 pause 时长（按服务 SLA 重要性）
4. 在 staging + prod 的 `kustomization.yaml` 把 `<svc>/deployment.yaml` 改成 `<svc>/rollout.yaml`
5. 把对应 `patches:` 段的 `kind: Deployment` 改成 `kind: Rollout`
6. dev 保持 Deployment（避免本地开发需要装 Argo Rollouts controller）
7. PR 评审通过后合并 → ArgoCD 自动 sync 到集群

## 故障排查

| 现象 | 可能原因 | 排查 |
|------|---------|------|
| `kubectl argo rollouts` 命令找不到 | CLI 未装 | 见前置条件 |
| Rollout 卡在 step 1 不动 | analysis 一直 `Running` | `kubectl describe analysisrun <name>` 看具体 metric |
| canary 5xx 飙升但没 abort | analysis 没引用对 template / metric 名错 | 检查 Rollout 的 `templates` 段 |
| `kubectl argo rollouts abort` 后流量没回 stable | APISIX upstream 缓存 | 重启 APISIX 或等 30s |
| 升级后调用方 502 | readinessProbe 没起作用 | 看 Pod ready 状态，dispatcher healthz 必须先过 |
