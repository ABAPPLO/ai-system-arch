# R2a spec — 四服务部署（notification / ai-gateway / billing + portal overlay）

日期：2026-07-17 · 分支 `fix/r2a-four-service-deploy`（新建）· 依据：fix-program 设计 §5 Wave 2 R2a（引用 §2.3）+ 审计「Phase 4 四服务代码写了、没部署」。

## 问题

Phase 4 四服务中：
- **notification / ai-gateway / billing**：有代码、有（除 notification 外）Dockerfile，但**完全没部署**——无 k8s manifest（ai-gateway/billing）、notification 仅有一个不完整的 deployment.yaml 且**缺 Dockerfile**；三个都不在任何 overlay 的 `resources:`、也不在 `scripts/kind/bootstrap.sh` 的构建/加载列表。
- **portal**：已在 kind overlay 部署并运行，但 **dev/staging/prod/prod-bj overlay 都没列它**（这 4 个 overlay 只列了原 12 服务）。

各 overlay 是**手列**每个服务 `configmap.yaml + deployment.yaml` 的 `resources:` 模型（无 base 统配）。staging/prod 部分服务用 Argo Rollout（canary）。

## 范围（已与用户确认）

- **主线 = kind e2e 实部署验证**：让 notification/ai-gateway/billing 在 kind 跑起来（pod Running + `/health/ready` 200 + 集群内 Service 可达）。
- **dev/staging/prod/prod-bj overlay**：补齐 4 服务（含 portal）的 `resources:` 引用，**Deployment 形式**（本轮不上 Rollout/canary，留后续）。这几个 overlay 仅 `kustomize build` 校验（本环境无对应集群，不实部署）。
- **portal**：kind 已部署不重做；仅补 dev/staging/prod/prod-bj overlay 引用。
- **不做功能/stub 兑现**：notification 的邮件/钉钉渠道（R2b）、ai-gateway 接 dispatcher 成唯一 AI 入口（R2c）、billing 业务逻辑——R2a 只**部署现有代码**让它健康跑起来。

## 走法（已定）

镜像既有 per-service manifest 模式（以 `deploy/k8s/services/api-registry/` 为模板），手写每个服务的 manifest 并手列进各 overlay `resources:`——与现有 12 服务一致。考虑过 kustomize component/base DRY，但现有服务全是手写、统一性优先，YAGNI 弃用。

### 模板参照
- **Dockerfile**（notification 用）：镜像 `services/services/billing/Dockerfile`——多阶段 `python:3.11-slim`，先 builder 装 `apihub-core` + 服务包，再 slim 层 `COPY --from=builder`，`CMD uvicorn <pkg>.main:app --port <N>`。改 `EXPOSE`/`CMD` 端口为 8012、workers 2。
- **deployment.yaml**：镜像 `deploy/k8s/services/api-registry/deployment.yaml`——单文件 3 文档（Deployment + Service + ServiceAccount）：replicas + RollingUpdate + labels + prometheus 注解（scrape/port 9090/path /metrics）+ `serviceAccountName` + pod `securityContext`（runAsNonRoot/runAsUser 1000/fsGroup/seccomp RuntimeDefault）+ container（image `registry.apihub.internal/apihub/<svc>:0.1.0-dev` + imagePullPolicy IfNotPresent + ports http/metrics + `envFrom`[configMapRef+secretRef+apihub-shared-infra+apihub-shared-secret] + resources + container securityContext[readOnlyRootFilesystem/allowPrivilegeEscalation false/capabilities drop ALL] + startupProbe `/health/ready`(5s×24) + readinessProbe + livenessProbe `/health/live` + volumeMount tmp）+ volumes emptyDir tmp；Service port 80→targetPort http；ServiceAccount（无 RoleBinding——这三个是普通 FastAPI 服务，不需 K8s API；仅 workflow/argo 需 RBAC）。
- **configmap.yaml**：镜像 `deploy/k8s/services/api-registry/configmap.yaml` + 对应依赖（见下）。
- **Kafka consumer 配置**（notification 用）：镜像 `deploy/k8s/services/retry/{configmap,deployment}.yaml` 的 Kafka 部分——configmap 加 `KAFKA_BROKERS` + 消费的 topic + `KAFKA_CONSUMER_GROUP`；deployment 加 `terminationGracePeriodSeconds: 60`（consumer 优雅退出）。

## 改动清单

### ① notification（缺 Dockerfile + manifest 不全）
- **新建** `services/services/notification/Dockerfile`：镜像 billing Dockerfile，端口 8012、`uvicorn notification.main:app --workers 2`。
- **新建** `deploy/k8s/services/notification/configmap.yaml`：PG + Kafka（`KAFKA_BROKERS` + consumer topic[s]（取自 `notification/consumer.py`）+ `KAFKA_CONSUMER_GROUP: notification`）+ OTel。Secret 占位（PG_PASSWORD 等，Sealed Secret 管）。
- **补全** `deploy/k8s/services/notification/deployment.yaml`：当前只有骨架（replicas 2、port 8012、startupProbe、Service）——按 api-registry 模板补：RollingUpdate、prometheus 注解、serviceAccountName、pod+container securityContext、resources、metrics port 9090、secretRef（notification-secret）+ shared infra/secret envFrom、readiness/liveness probe、tmp emptyDir、`terminationGracePeriodSeconds: 60`、ServiceAccount 文档。

### ② ai-gateway（有 Dockerfile，缺 manifest）
- **新建** `deploy/k8s/services/ai-gateway/configmap.yaml`：PG + OTel；AI provider 相关走 Secret（`ai_gateway_encryption_key` 已在 shared config；provider key 占位）。
- **新建** `deploy/k8s/services/ai-gateway/deployment.yaml`：按 api-registry 模板，端口 8013、replicas 2。

### ③ billing（有 Dockerfile，缺 manifest）
- **新建** `deploy/k8s/services/billing/configmap.yaml`：PG + ClickHouse（`CH_HOST` 等，镜像 trace/retry 的 CH 配置）+ OTel。
- **新建** `deploy/k8s/services/billing/deployment.yaml`：按 api-registry 模板，端口 8014、replicas 1（billing 单 worker，见其 Dockerfile）。

### ④ portal（kind 已部署，仅补 overlay）
- 不新增 manifest。dev/staging/prod/prod-bj overlay 补 `portal/configmap.yaml + deployment.yaml` 引用。

### ⑤ overlays（手列 resources）
- **kind**：`deploy/k8s/overlays/kind/kustomization.yaml` 加 notification/ai-gateway/billing 的 configmap+deployment（portal 已在）。
- **dev / staging / prod / prod-bj**：各 `kustomization.yaml` 加 notification/ai-gateway/billing/**portal** 的 configmap+deployment。本轮 Deployment 形式（staging/prod 不引 Rollout）。

### ⑥ bootstrap.sh
`scripts/kind/bootstrap.sh:147` 的 `SVC=(...)` 列表加 `notification ai-gateway billing`（否则 kind 不构建/加载这三个镜像 → ErrImagePull）。

## 验证（走真实入口）

- **kind e2e（实部署）**：
  1. `make docker-build SERVICE=notification|ai-gateway|billing` + `kind load`（或重跑 bootstrap）→ `scripts/k8s/apply.sh kind`。
  2. 3 服务 pod `Running` 0 restart；`/health/ready` 200（startupProbe 通过）。
  3. 集群内可达：`kubectl exec deploy/<任一> -- python -c "import httpx; print(httpx.get('http://notification.apihub-system/health/ready').status_code)"`（三个服务各探一次）。
  4. notification 的 Kafka consumer 启动不崩（logs 无 consumer traceback；`extra_lifespan` 起来）。
- **dev/staging/prod/prod-bj**：`kustomize build deploy/k8s/overlays/<env>` 各自成功（YAML 有效、无重复资源、无缺失引用）。
- **不回归**：`ruff` + 现有 `make test`（这三个服务有 tests/，跑过）。

## 风险

- **notification consumer 启动崩**：consumer 连 Kafka/消费 topic，若 configmap 缺 `KAFKA_CONSUMER_GROUP`/topic 名错 → `extra_lifespan` 抛异常 → pod CrashLoop。需对照 `notification/consumer.py` 的确切 topic/group（plan 阶段核实）。
- **billing 连 CH**：CH 在本 kind 栈 unhealthy-but-working（healthcheck 误报，实际可查，见 #51）。billing `/health/ready` 若强校验 CH 连通可能受影响——但 create_app 的 `/health/ready` 通常只查 pool 建连；核实。
- **readOnlyRootFilesystem + /tmp**：镜像 api-registry 用 emptyDir 挂 `/tmp`；新服务同样需要，否则写 /tmp 崩。
- **dev/staging/prod overlay 无 portal/notification/... 的 Secret**：这些环境走 Sealed Secret（git 里占位），本轮只动 ConfigMap+Deployment 引用，Secret 由各环境既定流程管——`kustomize build` 不校验 Secret 存在，不会挡。
- **镜像构建首次**：notification 首次构建 Dockerfile，注意 zlib1g-dev 等 build 依赖（#45 已为 ai-gateway/billing 修过，notification Dockerfile 镜像 billing 应已含）。

## 不做（R2a 边界）

- 不做 notification 渠道/模板（R2b）、ai-gateway 接 dispatcher（R2c）、billing 计费逻辑、GDPR（R2d）。
- 不上 staging/prod Rollout/canary（留后续）。
- 不动其他既有服务 manifest。
- 不部署到真实 dev/staging/prod 集群（无环境）。
