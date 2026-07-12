# staging-deploy Implementation Plan（prod 的镜像）

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development (或 executing-plans)。Steps use `- [ ]`。
> **Lean/delta-based**：staging 是 prod（PR #17，刚 merge）的结构镜像。每个 task 指明 prod 源文件 + staging 的精确替换；implementer 读 prod 文件 → 应用替换 → 验证。不重复 prod 全文。

**Goal:** 仿 prod-deploy 出 staging：`terraform/envs/staging` + `overlays/staging` 补全，静态可验证。

**Architecture / decisions:** 全部继承自 prod（PR #17）—— K8s Secret 占位 / kustomize `images:` transformer / +HPA(CPU)+Ingress(cert-manager) / envFrom 镜像 kind。staging 仅差异：mid 规格、staging 命名、ENV=staging。

**Mirror source（刚 merge，在 main）:** `deploy/terraform/envs/prod/*` + `deploy/k8s/overlays/prod/{shared-infra-prod,hpa,ingress,kustomization,patches/}` + `docs/superpowers/plans/2026-07-12-prod-deploy.md`。

**Spec:** 复用 `docs/superpowers/specs/2026-07-12-prod-deploy-design.md`（staging 为其镜像变种，无独立 spec）。

## Global Constraints（继承 prod + staging 差异）

- 静态验证 only（`terraform validate` + `kustomize build --load-restrictor LoadRestrictionsNone`），不 apply、不填真密码（`REPLACE_ME`）。
- region `cn-shanghai`，AZ `cn-shanghai-e/f/g`。
- **替换表（全 plan 适用，`prod`→`staging`）**：
  - 路径/文件名：`envs/prod`→`envs/staging`；`overlays/prod`→`overlays/staging`；`shared-infra-prod`→`shared-infra-staging`；`shared-secret-prod`→`shared-secret-staging`；`patches/*-envfrom.yaml` 指 `-staging` CM/Secret。
  - 标识符：`apihub-tfstate-prod`→`apihub-tfstate-staging`；`tflock_prod`→`tflock_staging`；`environment="prod"`→`"staging"`；`ENV=prod`→`ENV=staging`；`deployment.environment=prod`→`=staging`；image tag `prod-latest`→`staging-latest`；Ingress host `apihub.example.com`→`staging-apihub.example.com`。
  - **PG_SSL 保持 `verify-full`**（staging 是 pre-prod gate，mirror prod）。
- 分支 `feat/staging-deploy`；每 Task commit。

### Staging 规格（mid，介于 dev/prod 之间）

| 组件 | dev | prod | **staging** |
|---|---|---|---|
| ACK | 5×c7.2xlarge | 8×c7.4xlarge | **6×c7.2xlarge** |
| RDS | pg.n2.medium.2c/100 | pg.n2.large.2c/200 | **pg.n2.medium.2c/150** |
| Redis | master.small | master.large | **master.medium** |

### Staging replicas（HPA min/max 须协调；来自 overlays/staging/kustomization）

dispatcher 3 / executor 3 / quota 4 / retry 2 / auth 2（HPA 服务）。
HPA min/max（min≈replicas/2 向上取整≥1，max≈×2）：dispatcher 2/6、executor 2/6、quota 2/8、retry 1/4、auth 1/4。

---

### Task 1: terraform `deploy/terraform/envs/staging/`（mirror envs/prod）

**Files:** Create `envs/staging/{main,backend,providers,variables,outputs}.tf` + `terraform.tfvars.example`.
**Mirror source:** `deploy/terraform/envs/prod/*`.

- [ ] **Step 1:** 复制 `envs/prod/` 6 文件 → `envs/staging/`，按替换表改：`apihub-tfstate-prod`→`-staging`、`tflock_prod`→`tflock_staging`、`prefix="terraform/prod"`→`"terraform/staging"`、`environment` 默认 `"prod"`→`"staging"`、tfvars example 同理。
- [ ] **Step 2:** `main.tf` 应用 staging 规格（上表）：ack `node_count=6` + `node_instance_type="ecs.c7.2xlarge"`；rds `instance_type="pg.n2.medium.2c"` + `storage=150`；redis `redis.master.medium.default`；oss `apihub-${var.environment}-objects`（=apihub-staging-objects，自动）。
- [ ] **Step 3:** `outputs.tf` 字段名保持与 prod 一致（`kubeconfig`/`rds_endpoint`/`redis_host`/`kafka_brokers`），`value=` 引用同 prod（已对齐 module 实际 output）。
- [ ] **Step 4:** 验证 `cd deploy/terraform/envs/staging && terraform fmt -check && terraform init -backend=false && terraform validate` → `Success!`。
- [ ] **Step 5:** Commit `feat(terraform): envs/staging（mirror prod，staging 规格 + backend + outputs）`。

### Task 2: `overlays/staging/shared-infra-staging.yaml`（mirror prod）

**Files:** Create `deploy/k8s/overlays/staging/shared-infra-staging.yaml`.
**Mirror source:** `deploy/k8s/overlays/prod/shared-infra-prod.yaml`.

- [ ] **Step 1:** 复制 `shared-infra-prod.yaml` → `shared-infra-staging.yaml`，按替换表：CM 名 `apihub-shared-infra-prod`→`-staging`、Secret 名 `apihub-shared-secret-prod`→`-staging`。`PG_SSL=verify-full` 保持。Secret `stringData` 4 key（PG_PASSWORD/JWT_SECRET/REDIS_PASSWORD/CH_PASSWORD）全 `REPLACE_ME`。CM endpoints 占位同 prod（apply 时 terraform output 替）。
- [ ] **Step 2:** 验证 `python3 -c "import yaml;list(yaml.safe_load_all(open('deploy/k8s/overlays/staging/shared-infra-staging.yaml')));print('YAML OK')"`。
- [ ] **Step 3:** Commit `feat(k8s/staging): shared-infra-staging（CM 真 endpoints + Secret 占位）`。

### Task 3: `overlays/staging/hpa.yaml` + `ingress.yaml`（mirror prod）

**Files:** Create `deploy/k8s/overlays/staging/{hpa,ingress}.yaml`.
**Mirror source:** `deploy/k8s/overlays/prod/{hpa,ingress}.yaml`.

- [ ] **Step 1:** `hpa.yaml`：复制 prod，5 HPA（dispatcher→Rollout，余 Deployment），改 min/max 为 staging 值：dispatcher 2/6、executor 2/6、quota 2/8、retry 1/4、auth 1/4。`averageUtilization: 70` 不变。
- [ ] **Step 2:** `ingress.yaml`：复制 prod，host `apihub.example.com`→`staging-apihub.example.com`。ClusterIssuer `letsencrypt-prod` 保持（共用；注释注明）。其余（ACME http01、TLS secret名、backend apisix-gateway:80）不变。
- [ ] **Step 3:** 验证两文件 `python3 -c "import yaml;list(yaml.safe_load_all(open('...')))"` 各 `OK`。
- [ ] **Step 4:** Commit `feat(k8s/staging): HPA（5，staging min/max）+ Ingress/cert-manager TLS`。

### Task 4: `overlays/staging/kustomization.yaml` 接入 + 全量验证

**Files:** Modify `deploy/k8s/overlays/staging/kustomization.yaml` + Create `patches/*-envfrom.yaml`×11.
**Mirror source:** `deploy/k8s/overlays/prod/kustomization.yaml` + `overlays/prod/patches/`.

- [ ] **Step 1:** `resources:` 追加 `shared-infra-staging.yaml`/`hpa.yaml`/`ingress.yaml`。
- [ ] **Step 2:** `images:` transformer（11 服务 → ACR `:staging-latest`，即 prod 的 `:prod-latest`→`:staging-latest`，newName 同 ACR namespace）。
- [ ] **Step 3:** configMap patches（11）：`ENV=prod`→`ENV=staging`、`deployment.environment=prod`→`=staging`。
- [ ] **Step 4:** envFrom：镜像 `overlays/prod/patches/*-envfrom.yaml` → `overlays/staging/patches/`，CM/Secret 名 → `apihub-shared-infra-staging`/`apihub-shared-secret-staging`（dispatcher→Rollout）。kustomization `patches:` 引用之。
- [ ] **Step 5:** 全量验证：
```bash
kustomize build --load-restrictor LoadRestrictionsNone deploy/k8s/overlays/staging > /tmp/staging-render.yaml
grep -c "staging-latest" /tmp/staging-render.yaml | xargs echo "staging images (want 11):"
grep -c "kind: HorizontalPodAutoscaler" /tmp/staging-render.yaml | xargs echo "HPA (want 5):"
grep -c "ENV: staging" /tmp/staging-render.yaml | xargs echo "ENV=staging (want 11):"
grep -c "registry.cn-shanghai.aliyuncs.com.*prod-latest" /tmp/staging-render.yaml | xargs echo "residual prod tags (want 0):"
python3 -c "import yaml;[d for d in yaml.safe_load_all(open('/tmp/staging-render.yaml')) if d];print('render YAML OK')"
```
- [ ] **Step 6:** Commit `feat(k8s/staging): kustomization 接入 images/configMap/envFrom + 全量验证`。
- [ ] **Step 7:**（push/PR 按惯例等发话。）
