# prod-deploy 文件层 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 prod 部署的「文件层」补齐到静态可验证、填真值即可 apply：新建 `terraform/envs/prod` + 补 `overlays/prod`（config/secret/registry/HPA/Ingress-TLS）。

**Architecture:** terraform `envs/prod` mirror `envs/dev`（同 6 modules，prod 规格 + 独立 backend + outputs）。prod overlay 在既有 kustomization（已有 replicas/resources/Rollout）上加 3 个新资源文件 + `images:` transformer + configMap patches。验证 = `terraform validate` + `kustomize build`。

**Tech Stack:** Terraform 1.x + alicloud provider；Kustomize；cert-manager（CR 模板）；Argo Rollouts（已 wired）。

**Spec:** `docs/superpowers/specs/2026-07-12-prod-deploy-design.md`

## Global Constraints

- 静态验证 only，**不真 apply**（不跑 `terraform apply`、不连真云、不填真密码——占位 `REPLACE_ME`）。
- prod region = `cn-shanghai`（ADR-008 单 region）；AZ `cn-shanghai-e/f/g`（同 dev）。
- 所有新文件中文注释（与既有 overlay/terraform 风格一致）。
- overlay 经 `kustomize build --load-restrictor LoadRestrictionsNone` 验证（仓库 overlay 引父目录，见 `argocd-setup.sh` buildOptions）。
- 镜像：base 仍 `registry.apihub.internal/apihub/<svc>:0.1.0-dev`，prod overlay 用 `images:` transformer override 到 ACR 占位 `registry.cn-shanghai.aliyuncs.com/apihub/<svc>:prod-latest`。
- secrets 走 K8s Secret `stringData` 占位（`REPLACE_ME`），**不**引入 ExternalSecrets/KMS（non-goal）。
- HPA 只 CPU-based（Kafka-lag/KEDA 是 non-goal）。
- 分支 `feat/prod-deploy`；每 Task 末尾 commit。

---

### Task 1: terraform `deploy/terraform/envs/prod/`（mirror dev）

**Files:**
- Create: `deploy/terraform/envs/prod/main.tf`, `backend.tf`, `providers.tf`, `variables.tf`, `outputs.tf`, `terraform.tfvars.example`
- Reference (mirror source): `deploy/terraform/envs/dev/*.tf`

**Interfaces:**
- Consumes: modules `../../modules/{vpc,ack,rds,redis,kafka,oss}`（已存在）。
- Produces: `outputs.tf` 暴露 `kubeconfig` + `rds_endpoint` / `redis_host` / `kafka_brokers`（喂 Task 2 overlay ConfigMap）。

- [ ] **Step 1: `providers.tf`**

```hcl
provider "alicloud" {
  region = var.region
}
```

- [ ] **Step 2: `variables.tf`（镜像 dev，environment 默认 prod）**

```hcl
variable "region" {
  type    = string
  default = "cn-shanghai"
}

variable "environment" {
  type    = string
  default = "prod"
}

variable "rds_password" {
  type      = string
  sensitive = true
}
```
（dev/variables.tf 若还有别的 var，一并镜像。）

- [ ] **Step 3: `backend.tf`（prod 独立 state）**

```hcl
terraform {
  backend "oss" {
    bucket              = "apihub-tfstate-prod"
    prefix              = "terraform/prod"
    region              = "cn-shanghai"
    encrypt             = true
    tablestore_endpoint = "https://apihub-tflock.cn-shanghai.ots.aliyuncs.com"
    tablestore_table    = "tflock_prod"
  }
}
```

- [ ] **Step 4: `main.tf`（镜像 dev/main.tf 的 6 modules，prod delta）**

按 `envs/dev/main.tf` 逐 module 复制，仅改：
- `module "ack"`: `node_count = 8`（dev 5）；node_spec 升一档（如 ack-c2-large，按 dev 注释同类）。
- `module "rds"`: `instance_type = "pg.n2.large.2c"`（dev `pg.n2.medium.2c`）；`storage = 200`（dev 100）。
- `module "redis"`: `instance_class = "redis.master.large.default"`（dev small）。
- `module "oss"`: `bucket_name = "apihub-${var.environment}-objects"`（=apihub-prod-objects）。
- `module "vpc"` / `module "kafka"`: 同 dev（AZ `cn-shanghai-e/f/g`）。
- 各 module `environment = var.environment`（自动 prod）。

- [ ] **Step 5: `outputs.tf`（暴露 overlay 要用的 endpoints）**

```hcl
output "kubeconfig" {
  description = "prod ACK kubeconfig"
  value       = module.ack.kubeconfig
  sensitive   = true
}

output "rds_endpoint" {
  description = "RDS 连接地址（喂 overlay PG_HOST）"
  value       = module.rds.connection_string
}

output "redis_host" {
  description = "Redis 连接地址"
  value       = module.redis.host
}

output "kafka_brokers" {
  description = "Kafka bootstrap brokers"
  value       = module.kafka.bootstrap_brokers
}
```
（output 字段名以 `modules/<x>/outputs.tf` 实际为准——读之对齐。）

- [ ] **Step 6: `terraform.tfvars.example`**

```hcl
region      = "cn-shanghai"
environment = "prod"
# export TF_VAR_rds_password='xxxxx' 注入，不要硬编码
rds_password = "REPLACE_ME_WITH_STRONG_PASSWORD"
```

- [ ] **Step 7: 静态验证**

```bash
cd deploy/terraform/envs/prod
terraform fmt -check -diff
terraform init -backend=false
terraform validate
```
Expected: `Success! The configuration is valid.`；fmt 无 diff。

- [ ] **Step 8: Commit**

```bash
cd /home/applo/project/ai-system-arch
git add deploy/terraform/envs/prod/
git commit -m "feat(terraform): envs/prod（mirror dev，prod 规格 + 独立 backend + outputs）"
```

---

### Task 2: prod overlay `shared-infra-prod.yaml`（ConfigMap + Secret）

**Files:**
- Create: `deploy/k8s/overlays/prod/shared-infra-prod.yaml`
- Reference: `deploy/k8s/overlays/kind/shared-infra.yaml`（字段参考；prod 用真 endpoints）

**Interfaces:**
- Produces: ConfigMap `apihub-shared-infra-prod` + Secret `apihub-shared-secret-prod`，供各服务 envFrom（Task 4 接入，与 kind overlay envFrom patch 同模式）。

- [ ] **Step 1: 写 ConfigMap + Secret**

```yaml
# prod 数据层连接配置 + secrets（占位）。
# 与 kind 区别：prod 用真 cloud endpoints（确定性，提交 git，ArgoCD 管），不需 host.docker.internal + CoreDNS 运行时注入。
# endpoints 对齐 terraform envs/prod outputs（apply 时 terraform output 替换占位）。
apiVersion: v1
kind: ConfigMap
metadata:
  name: apihub-shared-infra-prod
  namespace: apihub-system
data:
  PG_HOST: "apihub-rds.internal"             # ← terraform output rds_endpoint
  PG_PORT: "5432"
  PG_USER: "apihub_app"
  PG_DATABASE: "apihub"
  PG_SSL: "verify-full"                       # prod 强制 SSL（kind=disable）
  PG_POOL_MIN: "10"
  PG_POOL_MAX: "50"
  REDIS_HOST: "apihub-redis.internal"         # ← terraform output redis_host
  REDIS_PORT: "6379"
  KAFKA_BROKERS: "apihub-kafka.internal:9092" # ← terraform output kafka_brokers
  CH_HOST: "apihub-clickhouse.internal"
  CH_PORT: "8123"
  CH_USERNAME: "apihub"
  OTEL_EXPORTER_OTLP_ENDPOINT: "http://otel-collector.apihub-monitoring:4317"
---
apiVersion: v1
kind: Secret
metadata:
  name: apihub-shared-secret-prod
  namespace: apihub-system
type: Opaque
# stringData：apply 时填真值（占位）。真 apply 前建议换 ExternalSecrets+KMS（non-goal）。
stringData:
  PG_PASSWORD: "REPLACE_ME"
  JWT_SECRET: "REPLACE_ME"
  REDIS_PASSWORD: "REPLACE_ME"
```

- [ ] **Step 2: 验证 YAML**

```bash
python3 -c "import yaml; list(yaml.safe_load_all(open('deploy/k8s/overlays/prod/shared-infra-prod.yaml'))); print('YAML OK')"
```
Expected: `YAML OK`

- [ ] **Step 3: Commit**

```bash
git add deploy/k8s/overlays/prod/shared-infra-prod.yaml
git commit -m "feat(k8s/prod): shared-infra-prod（ConfigMap 真 endpoints + Secret 占位）"
```

---

### Task 3: prod overlay `hpa.yaml` + `ingress.yaml`

**Files:**
- Create: `deploy/k8s/overlays/prod/hpa.yaml`, `ingress.yaml`
- Reference: prod replicas（dispatcher 8 / executor 10 / quota 8 / retry 3 / auth 3）

**Interfaces:**
- Produces: 5 HPA（dispatcher→Rollout，余→Deployment）+ Ingress（backend apisix-gateway）+ cert-manager ClusterIssuer。

- [ ] **Step 1: `hpa.yaml`（CPU@70%，min=max/2 max=×2）**

```yaml
# prod CPU-based HPA。Kafka-lag（retry/executor）需 KEDA，non-goal。
# dispatcher 是 Rollout（非 Deployment），scaleTargetRef.kind=Rollout。
---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: dispatcher
  namespace: apihub-system
spec:
  scaleTargetRef:
    apiVersion: argoproj.io/v1alpha1
    kind: Rollout
    name: dispatcher
  minReplicas: 4
  maxReplicas: 16
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 70
---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: executor
  namespace: apihub-system
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: executor
  minReplicas: 5
  maxReplicas: 20
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 70
---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: quota
  namespace: apihub-system
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: quota
  minReplicas: 4
  maxReplicas: 16
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 70
---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: retry
  namespace: apihub-system
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: retry
  minReplicas: 2
  maxReplicas: 6
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 70
---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: auth
  namespace: apihub-system
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: auth
  minReplicas: 2
  maxReplicas: 6
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 70
```

- [ ] **Step 2: `ingress.yaml`（Ingress + cert-manager ClusterIssuer）**

```yaml
# prod 入口：Ingress（占位域名）→ apisix-gateway；cert-manager ACME 自动 TLS。真域名/证书 non-goal。
---
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-prod
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: REPLACE_ME@example.com
    privateKeySecretRef:
      name: letsencrypt-prod-account-key
    solvers:
      - http01:
          ingress:
            class: nginx
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: apihub-ingress
  namespace: apihub-ingress
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod
spec:
  tls:
    - hosts: ["apihub.example.com"]
      secretName: apihub-tls
  rules:
    - host: "apihub.example.com"
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: apisix-gateway
                port:
                  number: 80
```

- [ ] **Step 3: 验证 YAML**

```bash
for f in hpa ingress; do python3 -c "import yaml; list(yaml.safe_load_all(open('deploy/k8s/overlays/prod/$f.yaml'))); print('$f OK')"; done
```
Expected: `hpa OK` / `ingress OK`

- [ ] **Step 4: Commit**

```bash
git add deploy/k8s/overlays/prod/hpa.yaml deploy/k8s/overlays/prod/ingress.yaml
git commit -m "feat(k8s/prod): HPA（5 服务 CPU-based）+ Ingress/cert-manager TLS"
```

---

### Task 4: `kustomization.yaml` 接入 + 全量验证

**Files:**
- Modify: `deploy/k8s/overlays/prod/kustomization.yaml`

**Interfaces:**
- Consumes: Task 2/3 新文件；既有 base services。
- Produces: `kustomize build overlays/prod` 渲染出完整 prod 集群（images→ACR、HPA/Ingress/Secret 在、configMap ENV=prod、PG_SSL=verify-full）。

- [ ] **Step 1: `resources:` 追加 3 文件**

在既有 `resources:` 末尾（`workflow/deployment.yaml` 后）加：
```yaml
  - shared-infra-prod.yaml
  - hpa.yaml
  - ingress.yaml
```

- [ ] **Step 2: 顶层加 `images:` transformer（11 服务 → ACR）**

`patches:` 段之后加：
```yaml
images:
  - name: registry.apihub.internal/apihub/api-registry
    newName: registry.cn-shanghai.aliyuncs.com/apihub/api-registry
    newTag: prod-latest
  - name: registry.apihub.internal/apihub/dispatcher
    newName: registry.cn-shanghai.aliyuncs.com/apihub/dispatcher
    newTag: prod-latest
  - name: registry.apihub.internal/apihub/auth
    newName: registry.cn-shanghai.aliyuncs.com/apihub/auth
    newTag: prod-latest
  - name: registry.apihub.internal/apihub/executor
    newName: registry.cn-shanghai.aliyuncs.com/apihub/executor
    newTag: prod-latest
  - name: registry.apihub.internal/apihub/quota
    newName: registry.cn-shanghai.aliyuncs.com/apihub/quota
    newTag: prod-latest
  - name: registry.apihub.internal/apihub/tenant
    newName: registry.cn-shanghai.aliyuncs.com/apihub/tenant
    newTag: prod-latest
  - name: registry.apihub.internal/apihub/admin
    newName: registry.cn-shanghai.aliyuncs.com/apihub/admin
    newTag: prod-latest
  - name: registry.apihub.internal/apihub/docs
    newName: registry.cn-shanghai.aliyuncs.com/apihub/docs
    newTag: prod-latest
  - name: registry.apihub.internal/apihub/trace
    newName: registry.cn-shanghai.aliyuncs.com/apihub/trace
    newTag: prod-latest
  - name: registry.apihub.internal/apihub/retry
    newName: registry.cn-shanghai.aliyuncs.com/apihub/retry
    newTag: prod-latest
  - name: registry.apihub.internal/apihub/workflow
    newName: registry.cn-shanghai.aliyuncs.com/apihub/workflow
    newTag: prod-latest
```

- [ ] **Step 3: configMap patches（ENV=prod + envFrom 接 shared-infra-prod）**

`patches:` 追加。11 服务 configmap 各加 ENV=prod + OTEL deployment.environment=prod（strategic merge on ConfigMap）。auth 为例：
```yaml
  - target:
      kind: ConfigMap
      name: auth-config
    patch: |-
      - op: add
        path: /data/ENV
        value: "prod"
      - op: add
        path: /data/OTEL_RESOURCE_ATTRIBUTES
        value: "deployment.environment=prod"
```
（11 服务同模式：api-registry/dispatcher/auth/executor/quota/tenant/admin/docs/trace/retry/workflow-config。）
envFrom 接 shared-infra-prod：与 kind overlay `patches/*-envfrom.yaml` 同手法——对每服务 Deployment patch `envFrom` 加 `configMapRef: apihub-shared-infra-prod` + `secretRef: apihub-shared-secret-prod`（base 已有 envFrom 段则 replace name，否则 add）。

- [ ] **Step 4: 全量 kustomize build 验证**

```bash
cd /home/applo/project/ai-system-arch
kustomize build --load-restrictor LoadRestrictionsNone deploy/k8s/overlays/prod > /tmp/prod-render.yaml
echo "rendered $(grep -c '^kind:' /tmp/prod-render.yaml) resources"
grep -c "registry.cn-shanghai.aliyuncs.com" /tmp/prod-render.yaml | xargs echo "ACR images (want 11):"
grep -c "kind: HorizontalPodAutoscaler" /tmp/prod-render.yaml | xargs echo "HPA (want 5):"
grep -c "kind: Ingress" /tmp/prod-render.yaml | xargs echo "Ingress (want 1):"
grep -c "apihub-shared-secret-prod" /tmp/prod-render.yaml | xargs echo "Secret refs:"
grep -c "verify-full" /tmp/prod-render.yaml | xargs echo "PG_SSL=verify-full:"
python3 -c "import yaml; [d for d in yaml.safe_load_all(open('/tmp/prod-render.yaml')) if d]; print('render YAML OK')"
```
Expected: ACR ≥11、HPA 5、Ingress 1、Secret refs >0、PG_SSL=verify-full ≥1、render YAML OK。

- [ ] **Step 5: Commit**

```bash
git add deploy/k8s/overlays/prod/kustomization.yaml
git commit -m "feat(k8s/prod): kustomization 接入 images transformer + 新 resources + configMap ENV=prod"
```

- [ ] **Step 6:（可选）开 PR**

按 push-on-ask，等用户发话再 push/squash-PR（一个 PR 覆盖 Task 1-4 + spec + 本 plan）。
