# R2a — 四服务部署（notification / ai-gateway / billing + portal overlay）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 让 notification / ai-gateway / billing 三个「有代码没部署」的服务在 kind 跑起来并 e2e 验证；dev/staging/prod/prod-bj overlay 补齐 4 服务（含 portal）资源引用。

**Architecture:** 镜像既有 per-service manifest 模式（`deploy/k8s/services/api-registry/` 为模板），手写每个服务的 `configmap.yaml`+`deployment.yaml`（Deployment+Service+ServiceAccount 单文件），手列进各 overlay `resources:`。kind 的 PG/Redis/Kafka/CH 真实地址由 `apihub-shared-infra` ConfigMap + `gen-envfrom-patches.py` 生成的 envFrom patch 注入（git-truth configmap 用占位 endpoint）。

**Tech Stack:** Kustomize（base+overlays，无 Helm）/ kind / docker / Python 3.11-slim 镜像。

**Spec:** `docs/superpowers/specs/2026-07-17-r2a-four-service-deploy-design.md`。

## Global Constraints

- **镜像 api-registry manifest 模式**：`deployment.yaml` 单文件 3 文档（Deployment + Service + ServiceAccount）。Deployment 含：replicas + RollingUpdate(maxSurge 1/maxUnavailable 0) + labels(`app.kubernetes.io/name`+`part-of: apihub`) + prometheus 注解(scrape true/port 9090/path /metrics) + `serviceAccountName: <svc>` + pod securityContext(runAsNonRoot/runAsUser 1000/fsGroup 1000/seccomp RuntimeDefault) + container[image `registry.apihub.internal/apihub/<svc>:0.1.0-dev` + imagePullPolicy IfNotPresent + ports(http containerPort N + metrics 9090) + envFrom(仅 `<svc>-config` + `<svc>-secret` 两个 ref；shared-infra 由 kind patch 注入) + resources + container securityContext(readOnlyRootFilesystem true/allowPrivilegeEscalation false/capabilities drop ALL) + startupProbe /health/ready(5s×24) + readinessProbe(/health/ready,initial 5,period 5) + livenessProbe(/health/live,initial 15,period 10) + volumeMount tmp(/tmp)] + volumes emptyDir tmp。Service port 80→targetPort http。ServiceAccount 无 RoleBinding。
- **git-truth configmap 用占位 endpoint**：`PG_HOST: apihub-rds.internal`、`REDIS_HOST: apihub-redis.internal`、`KAFKA_BROKERS: apihub-kafka-1:9092,...`、`CH_HOST: apihub-ch.internal`（prod/staging 由 terraform output 覆盖；kind 由 shared-infra envFrom patch 覆盖成 host.docker.internal）。镜像 trace/retry/api-registry configmap。
- **端口/副本/workers**：notification 8012 / replicas 2 / workers 2 / 有 Kafka consumer；ai-gateway 8013 / replicas 2 / workers 1；billing 8014 / replicas 1 / workers 1。
- **notification consumer**：`consumer.py:67` 硬编码 `_client.consumer("api-call-events", group="notification-svc")` → configmap 只需 `KAFKA_BROKERS`（topic/group 在代码里）；deployment 加 `terminationGracePeriodSeconds: 60`（consumer 优雅退出，镜像 retry deployment）。
- **billing 需 CH env**：configmap 含 `CH_HOST/CH_PORT/CH_USERNAME/CH_DATABASE/CH_POOL_SIZE`（镜像 trace configmap）。
- **kind envFrom（关键，否则 CrashLoop）**：新服务必须能拿 shared-infra。机制——`scripts/kind/gen-envfrom-patches.py` 的 `SERVICES` 列表 + 生成 `deploy/k8s/overlays/kind/patches/<svc>-envfrom.yaml` + kind `kustomization.yaml` 的 `patches:` 段加 target。portal 已手动加过（不在脚本 SERVICES 里但有 patch 文件 + target）。新服务照 portal 做法。
- **dev/staging/prod/prod-bj overlay**：本轮 **Deployment 形式**（staging/prod 不引 Rollout/canary）。仅加 `resources:` 引用，不实部署（本环境无集群），靠 `kustomize build` 校验。
- **bootstrap.sh**：`SVC=(...)` 列表（line 147）加 `notification ai-gateway billing`，否则 kind 不构建/加载 → ErrImagePull。
- **commit 粒度**：每 Task 末尾一次 commit；本轮一个 squash-PR。
- **GateGuard**：每文件首条 bash/edit 拦，陈述 facts 后重试。

---

## File Structure

**新建：**
- `services/services/notification/Dockerfile`
- `deploy/k8s/services/notification/configmap.yaml`（notification 现有 deployment.yaml 补全，不新建）
- `deploy/k8s/services/ai-gateway/configmap.yaml`、`deploy/k8s/services/ai-gateway/deployment.yaml`
- `deploy/k8s/services/billing/configmap.yaml`、`deploy/k8s/services/billing/deployment.yaml`
- `deploy/k8s/overlays/kind/patches/notification-envfrom.yaml`、`ai-gateway-envfrom.yaml`、`billing-envfrom.yaml`（由 gen-envfrom-patches.py 生成）

**修改：**
- `deploy/k8s/services/notification/deployment.yaml`（补全骨架）
- `deploy/k8s/overlays/kind/kustomization.yaml`（resources + patches target）
- `deploy/k8s/overlays/{dev,staging,prod,prod-bj}/kustomization.yaml`（resources 加 4 服务）
- `scripts/kind/bootstrap.sh:147`（SVC 列表）
- `scripts/kind/gen-envfrom-patches.py`（SERVICES 加 3 服务——便于后续重生成；portal 不在脚本里但已手加，保持）

---

## Task 1: notification（Dockerfile + configmap + 补全 deployment）

**Files:**
- Create: `services/services/notification/Dockerfile`、`deploy/k8s/services/notification/configmap.yaml`
- Modify: `deploy/k8s/services/notification/deployment.yaml`

- [ ] **Step 1: 写 notification Dockerfile（镜像 billing，端口/workers 改）**

创建 `services/services/notification/Dockerfile`：
```dockerfile
FROM python:3.11-slim AS builder
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends build-essential libpq-dev libffi-dev zlib1g-dev && rm -rf /var/lib/apt/lists/*
RUN useradd -m -u 1000 apihub && chown -R apihub:apihub /app
USER apihub
COPY --chown=apihub:apihub services/libs/apihub-core /tmp/apihub-core
RUN pip install --user /tmp/apihub-core
COPY --chown=apihub:apihub services/services/notification /tmp/notification
RUN pip install --user /tmp/notification
FROM python:3.11-slim
WORKDIR /app
COPY --from=builder /home/apihub/.local /root/.local
ENV PATH=/root/.local/bin:$PATH
EXPOSE 8012
CMD ["uvicorn", "notification.main:app", "--host", "0.0.0.0", "--port", "8012", "--workers", "2"]
```

- [ ] **Step 2: 写 notification configmap（PG + Kafka + OTel）**

创建 `deploy/k8s/services/notification/configmap.yaml`（镜像 api-registry configmap 结构；Secret 占位段同 api-registry）：
```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: notification-config
  namespace: apihub-system
  labels:
    app.kubernetes.io/name: notification
    app.kubernetes.io/part-of: apihub
data:
  APP_NAME: notification
  ENV: dev
  LOG_LEVEL: INFO
  PG_HOST: apihub-rds.internal
  PG_PORT: "5432"
  PG_DATABASE: apihub
  PG_POOL_MIN: "5"
  PG_POOL_MAX: "20"
  REDIS_HOST: apihub-redis.internal
  REDIS_PORT: "6379"
  KAFKA_BROKERS: apihub-kafka-1:9092,apihub-kafka-2:9092,apihub-kafka-3:9092
  KAFKA_TOPIC_CALL_EVENTS: api-call-events
  OTEL_EXPORTER_OTLP_ENDPOINT: http://otel-collector.apihub-monitoring:4317
  OTEL_SERVICE_NAME: notification
  OTEL_RESOURCE_ATTRIBUTES: deployment.environment=dev
---
apiVersion: v1
kind: Secret
metadata:
  name: notification-secret
  namespace: apihub-system
type: Opaque
# 真实环境用 Sealed Secrets 加密，这里仅占位
# stringData:
#   PG_PASSWORD: ...
```

- [ ] **Step 3: 补全 notification deployment.yaml**

把现有 `deploy/k8s/services/notification/deployment.yaml`（仅骨架）替换为镜像 api-registry 的完整模板，替换：`api-registry`→`notification`、containerPort 8000→`8012`、replicas 3→`2`、image→`registry.apihub.internal/apihub/notification:0.1.0-dev`、envFrom configMapRef/secretRef→`notification-config`/`notification-secret`、serviceAccountName→`notification`。**额外**（consumer）：在 `spec.template.spec` 加 `terminationGracePeriodSeconds: 60`。Service port 80→targetPort http、ServiceAccount name `notification`。

参照模板：`deploy/k8s/services/api-registry/deployment.yaml`（逐字段对照，含 prometheus 注解、pod+container securityContext、resources requests cpu 250m/memory 256Mi / limits cpu 1000m/memory 1Gi（notification 比 api-registry 轻）、startupProbe 5s×24、readiness/liveness、tmp emptyDir）。

- [ ] **Step 4: 校验**

Run: `python -c "import yaml; list(yaml.safe_load_all(open('deploy/k8s/services/notification/configmap.yaml'))); list(yaml.safe_load_all(open('deploy/k8s/services/notification/deployment.yaml'))); print('yaml ok')"`
Expected: `yaml ok`。

- [ ] **Step 5: Commit**

```bash
git add services/services/notification/Dockerfile deploy/k8s/services/notification/configmap.yaml deploy/k8s/services/notification/deployment.yaml
git commit -m "feat(r2a): notification Dockerfile + configmap + 完整 deployment"
```

---

## Task 2: ai-gateway（configmap + deployment）

**Files:** Create `deploy/k8s/services/ai-gateway/configmap.yaml`、`deployment.yaml`

- [ ] **Step 1: configmap（PG + OTel；ai-gateway 加密 key 走 Secret）**

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: ai-gateway-config
  namespace: apihub-system
  labels:
    app.kubernetes.io/name: ai-gateway
    app.kubernetes.io/part-of: apihub
data:
  APP_NAME: ai-gateway
  ENV: dev
  LOG_LEVEL: INFO
  PG_HOST: apihub-rds.internal
  PG_PORT: "5432"
  PG_DATABASE: apihub
  PG_POOL_MIN: "5"
  PG_POOL_MAX: "20"
  OTEL_EXPORTER_OTLP_ENDPOINT: http://otel-collector.apihub-monitoring:4317
  OTEL_SERVICE_NAME: ai-gateway
  OTEL_RESOURCE_ATTRIBUTES: deployment.environment=dev
---
apiVersion: v1
kind: Secret
metadata:
  name: ai-gateway-secret
  namespace: apihub-system
type: Opaque
# stringData:
#   PG_PASSWORD: ...
#   AI_GATEWAY_ENCRYPTION_KEY: <32-byte hex>
```

- [ ] **Step 2: deployment.yaml（镜像 api-registry，port 8013 / replicas 2 / workers 1）**

镜像 api-registry deployment.yaml，替换：name→`ai-gateway`、containerPort→`8013`、replicas→`2`、image→`registry.apihub.internal/apihub/ai-gateway:0.1.0-dev`、envRef→`ai-gateway-config`/`ai-gateway-secret`、serviceAccountName→`ai-gateway`。Service + ServiceAccount 同名。（ai-gateway Dockerfile 已 workers 1。）

- [ ] **Step 3: 校验 + Commit**

```bash
python -c "import yaml; [list(yaml.safe_load_all(open(f))) for f in ['deploy/k8s/services/ai-gateway/configmap.yaml','deploy/k8s/services/ai-gateway/deployment.yaml']]; print('yaml ok')"
git add deploy/k8s/services/ai-gateway/
git commit -m "feat(r2a): ai-gateway configmap + deployment"
```

---

## Task 3: billing（configmap + deployment）

**Files:** Create `deploy/k8s/services/billing/configmap.yaml`、`deployment.yaml`

- [ ] **Step 1: configmap（PG + CH + OTel，CH 镜像 trace）**

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: billing-config
  namespace: apihub-system
  labels:
    app.kubernetes.io/name: billing
    app.kubernetes.io/part-of: apihub
data:
  APP_NAME: billing
  ENV: dev
  LOG_LEVEL: INFO
  PG_HOST: apihub-rds.internal
  PG_PORT: "5432"
  PG_DATABASE: apihub
  PG_POOL_MIN: "2"
  PG_POOL_MAX: "10"
  CH_HOST: apihub-ch.internal
  CH_PORT: "8123"
  CH_USERNAME: apihub
  CH_DATABASE: apihub
  CH_POOL_SIZE: "10"
  OTEL_EXPORTER_OTLP_ENDPOINT: http://otel-collector.apihub-monitoring:4317
  OTEL_SERVICE_NAME: billing
  OTEL_RESOURCE_ATTRIBUTES: deployment.environment=dev
---
apiVersion: v1
kind: Secret
metadata:
  name: billing-secret
  namespace: apihub-system
type: Opaque
# stringData:
#   PG_PASSWORD: ...
#   CH_PASSWORD: ...
```

- [ ] **Step 2: deployment.yaml（port 8014 / replicas 1）**

镜像 api-registry deployment.yaml，替换：name→`billing`、containerPort→`8014`、replicas→`1`、image→`registry.apihub.internal/apihub/billing:0.1.0-dev`、envRef→`billing-config`/`billing-secret`、serviceAccountName→`billing`。

- [ ] **Step 3: 校验 + Commit**

```bash
python -c "import yaml; [list(yaml.safe_load_all(open(f))) for f in ['deploy/k8s/services/billing/configmap.yaml','deploy/k8s/services/billing/deployment.yaml']]; print('yaml ok')"
git add deploy/k8s/services/billing/
git commit -m "feat(r2a): billing configmap + deployment"
```

---

## Task 4: kind overlay wiring（resources + envFrom patches）

**Files:** Modify `deploy/k8s/overlays/kind/kustomization.yaml`、`scripts/kind/gen-envfrom-patches.py`；生成 `deploy/k8s/overlays/kind/patches/{notification,ai-gateway,billing}-envfrom.yaml`

- [ ] **Step 1: gen-envfrom-patches.py SERVICES 加 3 服务**

`scripts/kind/gen-envfrom-patches.py` 的 `SERVICES = [...]`（line 13-25）末尾加：
```python
    "notification",
    "ai-gateway",
    "billing",
```

- [ ] **Step 2: 重生成 patches**

Run: `python scripts/kind/gen-envfrom-patches.py`
Expected: 输出 `wrote 14 patches to deploy/k8s/overlays/kind/patches`（原 11 + portal 不在脚本里 + 3 新 = 14），且 `patches/notification-envfrom.yaml`、`ai-gateway-envfrom.yaml`、`billing-envfrom.yaml` 生成。

- [ ] **Step 3: kind kustomization 加 resources + patches target**

`deploy/k8s/overlays/kind/kustomization.yaml`：
- `resources:` 段加（portal 已在，加 3 新）：
```yaml
  - ../../services/notification/configmap.yaml
  - ../../services/notification/deployment.yaml
  - ../../services/ai-gateway/configmap.yaml
  - ../../services/ai-gateway/deployment.yaml
  - ../../services/billing/configmap.yaml
  - ../../services/billing/deployment.yaml
```
- `patches:` 段加（仿 portal line 69-70）：
```yaml
  - path: patches/notification-envfrom.yaml
    target: { kind: Deployment, name: notification }
  - path: patches/ai-gateway-envfrom.yaml
    target: { kind: Deployment, name: ai-gateway }
  - path: patches/billing-envfrom.yaml
    target: { kind: Deployment, name: billing }
```

- [ ] **Step 4: 校验 kind overlay build**

Run: `kustomize build --load-restrictor LoadRestrictionsNone deploy/k8s/overlays/kind > /tmp/r2a-kind.yaml && echo "build ok ($(grep -c 'kind: Deployment' /tmp/r2a-kind.yaml) deployments)"`
Expected: `build ok` + deployment 数 ≥ 15（原 12 + portal + 3 新）。检查 3 新 Deployment 的 envFrom 含 `apihub-shared-infra` + `apihub-shared-secret`：
Run: `grep -A6 'name: notification$' /tmp/r2a-kind.yaml | grep shared` → 应见 shared-infra/shared-secret。

- [ ] **Step 5: Commit**

```bash
git add scripts/kind/gen-envfrom-patches.py deploy/k8s/overlays/kind/kustomization.yaml deploy/k8s/overlays/kind/patches/notification-envfrom.yaml deploy/k8s/overlays/kind/patches/ai-gateway-envfrom.yaml deploy/k8s/overlays/kind/patches/billing-envfrom.yaml
git commit -m "feat(r2a): kind overlay 资源 + envFrom patches（notification/ai-gateway/billing）"
```

---

## Task 5: dev/staging/prod/prod-bj overlay（含 portal）

**Files:** Modify `deploy/k8s/overlays/{dev,staging,prod,prod-bj}/kustomization.yaml`

- [ ] **Step 1: 4 overlay 各加 4 服务 resources（Deployment 形式）**

对 `dev`、`staging`、`prod`、`prod-bj` 的 `kustomization.yaml`，在 `resources:` 段（参照既有服务块）加：
```yaml
  - ../../services/notification/configmap.yaml
  - ../../services/notification/deployment.yaml
  - ../../services/ai-gateway/configmap.yaml
  - ../../services/ai-gateway/deployment.yaml
  - ../../services/billing/configmap.yaml
  - ../../services/billing/deployment.yaml
  - ../../services/portal/configmap.yaml
  - ../../services/portal/deployment.yaml
```
注意：staging/prod 现有部分服务用 `rollout.yaml`——本轮 4 新服务**只引 deployment.yaml**（不上 Rollout，spec 边界）。

- [ ] **Step 2: 校验 4 overlay build**

```bash
for o in dev staging prod prod-bj; do
  kustomize build --load-restrictor LoadRestrictionsNone deploy/k8s/overlays/$o >/tmp/r2a-$o.yaml && echo "$o build ok"
done
```
Expected: 4 个 `build ok`。各 overlay 的 deployment 数 ≥ 16（原 12 + 4 新）。

- [ ] **Step 3: Commit**

```bash
git add deploy/k8s/overlays/dev/kustomization.yaml deploy/k8s/overlays/staging/kustomization.yaml deploy/k8s/overlays/prod/kustomization.yaml deploy/k8s/overlays/prod-bj/kustomization.yaml
git commit -m "feat(r2a): dev/staging/prod/prod-bj overlay 引用 4 服务（Deployment 形式）"
```

---

## Task 6: bootstrap.sh SVC 列表

**Files:** Modify `scripts/kind/bootstrap.sh:147`

- [ ] **Step 1: SVC 数组加 3 服务**

把 `SVC=(api-registry dispatcher auth executor quota tenant admin docs trace retry workflow portal)` 改为末尾加 `notification ai-gateway billing`：
```bash
SVC=(api-registry dispatcher auth executor quota tenant admin docs trace retry workflow portal notification ai-gateway billing)
```

- [ ] **Step 2: 校验 shell**

Run: `bash -n scripts/kind/bootstrap.sh && echo ok`
Expected: `ok`。

- [ ] **Step 3: Commit**

```bash
git add scripts/kind/bootstrap.sh
git commit -m "chore(r2a): bootstrap SVC 列表加 notification/ai-gateway/billing"
```

---

## Task 7: kind e2e 验证（实部署）

前置：kind-apihub 在跑、dev 栈（PG/Redis/Kafka/CH）在跑。本 Task 不写代码，跑验证记入 PR。

- [ ] **Step 1: 构建+加载 3 镜像**

```bash
for s in notification ai-gateway billing; do
  docker build -f services/services/$s/Dockerfile -t registry.apihub.internal/apihub/$s:0.1.0-dev .
  kind load docker-image registry.apihub.internal/apihub/$s:0.1.0-dev --name apihub
done
```

- [ ] **Step 2: apply kind**

```bash
scripts/k8s/apply.sh kind 2>&1 | tail -20
```

- [ ] **Step 3: 3 服务 pod 健康**

```bash
for s in notification ai-gateway billing; do
  kubectl --context kind-apihub -n apihub-system rollout status deploy/$s --timeout=180s
  kubectl --context kind-apihub -n apihub-system get pod -l app.kubernetes.io/name=$s
done
```
Expected: 每个 `successfully rolled out`，pod `Running` 0 restart。

- [ ] **Step 4: /health/ready 200 + Service 可达**

```bash
for s in notification ai-gateway billing; do
  kubectl --context kind-apihub -n apihub-system exec deploy/$s -- python -c "import httpx; print('$s', httpx.get('http://127.0.0.1/health/ready').status_code)"
  kubectl --context kind-apihub -n apihub-system exec deploy/api-registry -- python -c "import httpx; print('$s-svc', httpx.get('http://$s.apihub-system/health/ready').status_code)"
done
```
Expected: 每行 `200`（本地 + Service DNS 各 200）。

- [ ] **Step 5: notification consumer 起来不崩**

```bash
kubectl --context kind-apihub -n apihub-system logs deploy/notification --tail=30 | grep -iE 'consumer|error|traceback|started' | tail
```
Expected: 见 consumer 启动日志，无 Traceback。

- [ ] **Step 6: 全量 kustomize build 回归**

```bash
for o in kind dev staging prod prod-bj; do kustomize build --load-restrictor LoadRestrictionsNone deploy/k8s/overlays/$o >/dev/null && echo "$o ok"; done
```
Expected: 5 个 ok。

- [ ] **Step 7: 更新进度记忆 + 开 PR**

更新 `~/.claude/.../memory/apihub-fix-program-progress.md`（R2a 状态）；`git push` 后开 squash-PR（merge 仅在 ask）。

---

## Self-Review 结论

- **Spec 覆盖**：① notification Dockerfile+configmap+deployment（Task 1）✓；② ai-gateway configmap+deployment（Task 2）✓；③ billing configmap+deployment（Task 3）✓；④ overlays（kind resources+envFrom Task 4；dev/staging/prod/prod-bj 含 portal Task 5）✓；⑤ bootstrap SVC（Task 6）✓；⑥ 验证 kind e2e + kustomize（Task 7）✓。
- **关键坑已落地**：kind envFrom（gen-envfrom-patches SERVICES + 重生成 + patches target，Task 4）——否则 3 服务连不上 PG/Redis → CrashLoop（这是 spec 风险栏的核心）。consumer terminationGracePeriod、billing CH env、readOnlyRootFilesystem+emptyDir 均覆盖。
- **无占位符**：configmap/Dockerfile 给全量；deployment.yaml 因 3 服务近乎同构，用「镜像 api-registry + 替换表」（指向真实模板文件，比转述 90 行更可靠）——替换字段全列。
- **类型/字段一致**：端口（8012/8013/8014）、envFrom ref 名（`<svc>-config`/`<svc>-secret`）、ServiceAccount 名与 serviceAccountName 一致；kind patch target name 与 Deployment name 一致。
