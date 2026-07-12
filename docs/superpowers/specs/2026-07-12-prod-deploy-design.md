# Spec — 生产部署文件层（overlays/prod 补全 + Terraform envs/prod）

> 日期：2026-07-12 · 分支 `feat/prod-deploy`
> 范围：B 段生产部署 critical path 的**文件层**——补 `overlays/prod` 缺口 + 新建 `terraform/envs/prod`。
> **静态验证 only**（`terraform validate/fmt` + `kustomize build`），**不真上云 apply**（需 creds/$，另轮）。
> 前置：handoff-2026-07-12-k8s-links-hardening（dev/kind 验证已 5/5 闭环）。

## Context（为什么）

dev/kind 验证闭环（k8s-links 5/5），下一里程碑 = 真生产部署。现状：

- `deploy/k8s/overlays/prod/kustomization.yaml` **已存在**：全 prod replicas/resources + dispatcher-as-Rollout（canary）+ argo-rollouts analysis-templates。
- `deploy/terraform/` 是真 alicloud IaC（6 modules：vpc/ack/rds/redis/kafka/oss），但 `envs/` 只有 `dev/`——**`staging/`、`prod/` 是 TODO**（README 标注）。
- prod overlay 缺：镜像 registry/tag、prod configMap 值（`PG_SSL=verify-full` / `ENV=prod` / 真 endpoints）、secrets、HPA、Ingress/TLS。

本轮把 prod 的「文件层」补齐到「静态可验证、填真值即可 apply」的程度。

## Goals

1. 新建 `deploy/terraform/envs/prod/`（mirror dev，prod 规格 + backend + outputs）。
2. 补 `deploy/k8s/overlays/prod/`：prod configMap/secret + 镜像 registry transformer + HPA + Ingress/TLS。
3. 静态验证通过：`terraform validate`（envs/prod）+ `kustomize build overlays/prod`（renders 无报错）。

## Non-goals（明确不做）

- 真 `terraform apply`（需 ALICLOUD creds + 真实资源费用 + state backend bucket 预创建）。
- ExternalSecrets / KMS / Vault（secrets 走 K8s Secret 占位，apply 时填）。
- KEDA / Kafka-lag HPA（HPA 只做 CPU-based）。
- staging env（结构同 prod 镜像，快速 followup）。
- 真域名 / 证书签发（Ingress 用占位域名 + cert-manager ACME Issuer 模板）。
- ArgoCD prod Application（GitOps 闭环——可本轮收尾也可下轮，见 Open questions）。

## Decisions（本轮 gating 选择）

| 决策点 | 选择 | 理由 |
|---|---|---|
| 范围 | 只写文件，静态验证 | 无需 creds/$；把 prod 拉到「差真值即可 apply」 |
| secrets | K8s Secret 占位（stringData，`REPLACE_ME`） | 最简、静态可验证、与 base secretRefs 一致；真 apply 时换 ExternalSecrets/KMS |
| 镜像 registry | kustomize `images:` transformer | 不侵入 base（base 仍 `registry.apihub.internal`），apply 时 override 成 ACR |
| overlay 范围 | +Ingress/TLS + HPA | 完整 prod（用户选定） |
| 数据层 endpoints | prod ConfigMap 提交 git（ArgoCD 管） | 真 cloud endpoints 确定性（不像 kind 需运行时注入）；host 非密，password 进 Secret |
| HPA | CPU-based | Kafka-lag 需 KEDA（另轮） |
| Ingress TLS | cert-manager ClusterIssuer（ACME） | 占位域名 + 自动签发模板 |

## Design

### ① terraform `deploy/terraform/envs/prod/`（新建，mirror dev）

文件（镜像 `envs/dev/`）：

- `main.tf` — 组合 6 modules，prod 规格：
  - `ack`: node_count ~8、更大 spec（vs dev 5）。
  - `rds`: instance_type 升级（如 `pg.n2.large.2c` / 主备 HA）、storage 加大（vs dev `pg.n2.medium.2c`/100）。
  - `redis`: `redis.master.large.default`（vs dev `master.small`）。
  - `kafka` / `oss`: prod 命名（`apihub-prod-objects`）。
  - AZ: `cn-shanghai-e/f/g`（同 dev，ADR-008 单 region）。
- `backend.tf` — OSS `apihub-tfstate-prod` + TableStore `tflock_prod`。
- `providers.tf` — `provider "alicloud" { region = var.region }`。
- `variables.tf` — `region` / `rds_password` / `environment = "prod"`。
- `outputs.tf` — `kubeconfig` + RDS/Redis/Kafka endpoints（**喂 overlay ConfigMap**）。
- `terraform.tfvars.example` — `region = "cn-shanghai"` + `rds_password` 占位。

### ② prod overlay 补全（`deploy/k8s/overlays/prod/`）

现状 `kustomization.yaml` 已有：resources（11 服务 + Rollout + analysis-templates）+ patches（replicas/resources）。补：

**新文件：**

- `shared-infra-prod.yaml` —
  - ConfigMap `apihub-shared-infra-prod`：真 endpoints（`apihub-rds.internal` 约定 / TF output refs）、`PG_SSL=verify-full`、prod pool（min 10 / max 50）、OTEL / Kafka / CH。
  - Secret `apihub-shared-secret-prod`（`stringData` 占位）：`PG_PASSWORD` / `JWT_SECRET` / `REDIS_PASSWORD`（值 `REPLACE_ME`，注释 apply 时填）。
  - 各服务经 envFrom 引用（与 kind overlay 同模式）。
- `hpa.yaml` — CPU-based HPA：dispatcher / executor / quota / retry / auth。min/max 与 overlay replicas 协调（如 min = replicas/2、max = replicas×2），`targetCPUUtilizationPercentage: 70`。
- `ingress.yaml` — Ingress（host `apihub.example.com` 占位）+ cert-manager `ClusterIssuer`（`letsencrypt-prod`，ACME HTTP-01）+ TLS secret ref。

**`kustomization.yaml` 编辑：**

- `images:` transformer（新增）— base `registry.apihub.internal/apihub/<svc>:0.1.0-dev` → ACR 占位（如 `registry.cn-shanghai.aliyuncs.com/apihub/<svc>:prod-latest`）。
- `resources:` 追加 `shared-infra-prod.yaml` / `hpa.yaml` / `ingress.yaml`。
- `patches:` 追加 configMap strategic-merge patch（各服务 configmap：`ENV=prod`、`OTEL_RESOURCE_ATTRIBUTES: deployment.environment=prod`）。

### ③ 静态验证

```bash
# terraform envs/prod
cd deploy/terraform/envs/prod && terraform fmt -check && terraform init -backend=false && terraform validate
# kustomize（本仓库 overlay 需 LoadRestrictionsNone，见 argocd-setup.sh buildOptions）
kustomize build --load-restrictor LoadRestrictionsNone deploy/k8s/overlays/prod >/tmp/prod-render.yaml
# 渲染后断言：images 已替换为 ACR、HPA/Ingress/Secret 在、configMap ENV=prod、PG_SSL=verify-full
```

## 文件清单

- 新建 `deploy/terraform/envs/prod/{main,backend,providers,variables,outputs}.tf` + `terraform.tfvars.example`（6 文件）。
- 新建 `deploy/k8s/overlays/prod/{shared-infra-prod,hpa,ingress}.yaml`（3 文件）。
- 编辑 `deploy/k8s/overlays/prod/kustomization.yaml`（images transformer + resources + configMap patches）。

## Open questions / followups

- **staging env**（mirror prod，mid 规格）—— 结构同，快速 followup。
- **KEDA + Kafka-lag HPA**（retry/executor 真实弹性）。
- **ExternalSecrets + KMS**（prod 真 secrets 管理）。
- **真 `terraform apply`**（creds + state bucket 预创建 + 真域名/证书签发）。
- **ArgoCD prod Application**（`deploy/argocd/prod.yaml`，path=overlays/prod，prod cluster）—— GitOps 闭环（本轮收尾 or 下轮）。
