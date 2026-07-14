# 多 Region 全双活 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 cn-shanghai 基础上增加 cn-beijing Region，实现租户亲和的全双活架构（写分区 + 读双活，RTO < 30s）。

**Architecture:** 租户亲和路由（每个租户固定 home_region）→ PG 双向逻辑订阅（按 tenant 拆 publication）→ Redis 独立 Cluster → Kafka MirrorMaker 双向。无状态服务双 Region 各部署一套。阿里云云解析 GSLB 就近解析 + 故障切换。

**Tech Stack:** Terraform (alicloud) · ACK (K8s) · RDS PG (逻辑复制) · Redis Cluster · Kafka MirrorMaker · ClickHouse · APISIX · ArgoCD · Thanos

## Global Constraints

- 所有 Terraform 变更走 `plan → PR → apply` 流程，不手动改云资源
- VPC CIDR 不冲突：sh=10.0.0.0/16, bj=10.1.0.0/16
- PG 逻辑订阅必须用 `origin = none` 防循环复制
- Redis Key 必须含 Region 前缀（`sh`/`bj`）
- APISIX 自定义插件必须走 etcd config，不用硬编码
- 存量 `tenant` 表加 `home_region` 字段，默认 `sh`
- 所有新 ConfigMap 必须加 `HOME_REGION` 环境变量
- Region B 初期按 50% 规格部署（15 ACK 节点、3 CH 节点）

---

## 文件清单

### 新文件

```
deploy/terraform/envs/prod-bj/
├── backend.tf
├── main.tf                  # 组合 modules（vpc/ack/rds/redis/kafka/oss）
├── outputs.tf
├── providers.tf
├── variables.tf
└── terraform.tfvars

deploy/k8s/overlays/prod-bj/
├── kustomization.yaml
├── ingress.yaml
├── hpa.yaml
├── shared-infra-bj.yaml
└── patches/
    └── configmap-patch.yaml

deploy/argocd/prod-bj.yaml

scripts/init-db/08-tenant-home-region.sql
scripts/multi-region/deploy-mirrormaker.sh
scripts/multi-region/setup-pg-logical-replication.sh
scripts/multi-region/failover-runbook.sh
scripts/multi-region/health-check-sh.sh
scripts/multi-region/health-check-bj.sh

deploy/apisix/plugins/tenant-affinity.lua

services/libs/apihub-core/src/apihub_core/config.py  (修改)
services/services/auth/src/auth/routes.py             (修改)
```

### 修改文件

| 文件 | 改动 |
|------|------|
| `deploy/terraform/modules/vpc/main.tf` | + VPC Peering 参数 |
| `deploy/k8s/base/monitoring/prometheus.yaml` | + remote_write 到 Thanos |
| `services/libs/apihub-core/src/apihub_core/config.py` | + `HOME_REGION` + `PEER_REGION_CH_HOST` |
| `services/services/auth/src/auth/routes.py` | `/internal/auth/check` 返回增加 `home_region` |
| `services/go/quota/internal/limiter/redis.go` | + region 前缀 + split ratio |
| `deploy/k8s/base/apigw/apisix-values.yaml` | + 自定义插件加载 |
| `docs/00-decisions.md` | ADR-008 → Superseded, 新增 ADR-013 |
| `docs/04-data-model.md` | tenant 表加 home_region |

---

### Phase A: 基础设施复制

### Task A1: Terraform 新增 prod-bj 环境

**Files:**
- Create: `deploy/terraform/envs/prod-bj/backend.tf`
- Create: `deploy/terraform/envs/prod-bj/main.tf`
- Create: `deploy/terraform/envs/prod-bj/outputs.tf`
- Create: `deploy/terraform/envs/prod-bj/providers.tf`
- Create: `deploy/terraform/envs/prod-bj/variables.tf`
- Create: `deploy/terraform/envs/prod-bj/terraform.tfvars.example`
- Modify: `deploy/terraform/modules/vpc/main.tf` (加 peering 参数)

**Interfaces:**
- Consumes: 已有 `modules/vpc` / `modules/ack` / `modules/rds` / `modules/redis` / `modules/kafka` / `modules/oss`
- Produces: CN-beijing 完整基础设施（VPC + ACK + RDS + Redis + Kafka + OSS）

- [ ] **Step 1: 复制 prod 结构作为模板**

```bash
cp -r deploy/terraform/envs/prod deploy/terraform/envs/prod-bj
```

- [ ] **Step 2: 编写 backend.tf（独立 OSS state）**

```hcl
terraform {
  backend "oss" {
    bucket         = "apihub-tfstate-bj"
    prefix         = "prod-bj"
    key            = "terraform.tfstate"
    region         = "cn-beijing"
    encrypt        = true
    acl            = "private"
  }
}
```

- [ ] **Step 3: 编写 main.tf（复用 modules，改 region + CIDR）**

```hcl
provider "alicloud" {
  region = "cn-beijing"
}

module "vpc" {
  source = "../../modules/vpc"
  vpc_cidr      = "10.1.0.0/16"
  vpc_name      = "apihub-prod-bj"
  azs           = ["cn-beijing-h", "cn-beijing-i", "cn-beijing-j"]
  enable_peering = true
  peer_vpc_id   = data.terraform_remote_state.sh.outputs.vpc_id
  peer_region   = "cn-shanghai"
}

module "ack" {
  source   = "../../modules/ack"
  name     = "apihub-prod-bj"
  vpc_id   = module.vpc.vpc_id
  node_spec = "ecs.c7.2xlarge"
  node_count = 15
}

module "rds" {
  source     = "../../modules/rds"
  name       = "apihub-prod-bj"
  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.data_subnet_ids
  spec       = "pg.x4.large.2c"
  storage_gb = 500
  logical_replication = true
}

module "redis" {
  source   = "../../modules/redis"
  name     = "apihub-prod-bj"
  vpc_id   = module.vpc.vpc_id
  shard_count = 8
  shard_capacity_gb = 2
}

module "kafka" {
  source     = "../../modules/kafka"
  name       = "apihub-prod-bj"
  vpc_id     = module.vpc.vpc_id
  broker_count = 6
  broker_spec  = "4c8g"
  disk_size_gb = 2000
}

module "oss" {
  source = "../../modules/oss"
  bucket = "apihub-prod-bj"
  region = "cn-beijing"
}
```

- [ ] **Step 4: 更新 vpc module 加 peering**

```hcl
# deploy/terraform/modules/vpc/main.tf 新增：
variable "enable_peering" { default = false }
variable "peer_vpc_id"    { default = "" }
variable "peer_region"    { default = "" }

resource "alicloud_vpc_peering_connection" "this" {
  count = var.enable_peering ? 1 : 0
  vpc_id       = alicloud_vpc.vpc.id
  peer_vpc_id  = var.peer_vpc_id
  peer_region  = var.peer_region
  bandwidth    = 1000
}
```

- [ ] **Step 5: terraform validate + plan**

```bash
cd deploy/terraform/envs/prod-bj
terraform init
terraform validate
terraform plan -out=tfplan
# 人工 review tfplan
terraform apply tfplan
```

Expected: CN-beijing VPC / ACK / RDS / Redis / Kafka / OSS 全部就绪。

- [ ] **Step 6: 验证跨 Region VPC Peering**

```bash
ping -c 3 <bj-rds-private-ip>
ping -c 3 <sh-rds-private-ip>
```

Expected: 双向 ping 通，延迟 < 5ms。

- [ ] **Step 7: 提交**

```bash
git add deploy/terraform/envs/prod-bj/ deploy/terraform/modules/vpc/main.tf
git commit -m "feat(multi-region): add prod-bj terraform env + vpc peering"
```

---

### Task A2: ArgoCD + K8s 双集群部署

**Files:**
- Create: `deploy/argocd/prod-bj.yaml`
- Create: `deploy/k8s/overlays/prod-bj/kustomization.yaml`
- Create: `deploy/k8s/overlays/prod-bj/ingress.yaml`
- Create: `deploy/k8s/overlays/prod-bj/hpa.yaml`
- Create: `deploy/k8s/overlays/prod-bj/shared-infra-bj.yaml`
- Create: `deploy/k8s/overlays/prod-bj/patches/configmap-patch.yaml`

**Interfaces:**
- Consumes: Task A1 的 ACK 集群（apihub-prod-bj）
- Produces: prod-bj K8s 就绪，ArgoCD 管理

- [ ] **Step 1: 复制 prod overlay 为 prod-bj 模板**

```bash
cp -r deploy/k8s/overlays/prod deploy/k8s/overlays/prod-bj
```

- [ ] **Step 2: 编写 prod-bj ArgoCD Application**

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: apihub-prod-bj
  namespace: argocd
spec:
  source:
    repoURL: git@:apihub-platform/apihub-deploy.git
    targetRevision: main
    path: k8s/overlays/prod-bj
  destination:
    server: https://<bj-ack-cluster-api-server>
    namespace: apihub-system
  syncPolicy:
    automated:
      prune: false
      selfHeal: false
    syncOptions:
      - CreateNamespace=true
```

- [ ] **Step 3: 编写 ConfigMap patch（Region B 连接串）**

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: apihub-config
data:
  HOME_REGION: "bj"
  PG_DSN: "postgresql://apihub:****@pg-bj.internal:5432/apihub"
  REDIS_URL: "redis://redis-bj.internal:6379"
  KAFKA_BROKERS: "kafka-bj-1:9092,kafka-bj-2:9092,kafka-bj-3:9092"
  CH_HOST: "http://ch-bj.internal:8123"
  PEER_REGION_CH_HOST: "http://ch-sh.internal:8123"
  QUOTA_REGION_SPLIT_RATIO: "0.4"
  PEER_QUOTA_REGION_SPLIT_RATIO: "0.6"
```

- [ ] **Step 4: 验证双集群同步**

```bash
argocd app sync apihub-prod-bj
kubectl --context=bj get pods -n apihub-system
```

Expected: 所有 service pods 在 bj 集群 Running，health endpoint 返回 200。

- [ ] **Step 5: 提交**

```bash
git add deploy/argocd/prod-bj.yaml deploy/k8s/overlays/prod-bj/
git commit -m "feat(multi-region): add prod-bj ArgoCD + K8s overlay"
```

---

### Task A3: APISIX 双 Region 部署 + DNS GSLB

**Files:**
- Create: `scripts/multi-region/health-check-sh.sh`
- Create: `scripts/multi-region/health-check-bj.sh`
- 阿里云云解析控制台操作

**Interfaces:**
- Consumes: Task A2 的 ACK 集群
- Produces: 双 Region APISIX 就绪 + GSLB 智能 DNS

- [ ] **Step 1: 在 Region B 部署 APISIX（复用 base 配置）**

```bash
kubectl --context=bj get pods -n apihub-ingress -l app.kubernetes.io/name=apisix
```

Expected: 3+ APISIX pod Running。

- [ ] **Step 2: 配置阿里云云解析 GSLB**

```
域名: api.apihub.com
解析策略: 智能 DNS（按源 IP 就近）
A 记录 1: cn-shanghai SLB IP（权重 100）
A 记录 2: cn-beijing SLB IP（权重 100）
健康检查路径: /health/ready
健康检查间隔: 10s
TTL: 30s
```

- [ ] **Step 3: 编写健康检查脚本**

```bash
#!/bin/bash
# scripts/multi-region/health-check-sh.sh
curl -sf -o /dev/null http://localhost:8001/health/ready || exit 1
REPLAY_LAG=$(psql -Atc "SELECT EXTRACT(epoch FROM replay_lag) FROM pg_stat_wal_receiver")
if [ "$REPLAY_LAG" -gt 30 ]; then exit 1; fi
exit 0
```

- [ ] **Step 4: 验证双 Region 路由**

```bash
curl -sI https://api.apihub.com/v1/health/live | grep -i "x-region"
```

Expected: 各 Region 返回对应的 region header。

- [ ] **Step 5: 提交**

```bash
git add scripts/multi-region/
git commit -m "feat(multi-region): APISIX dual-region + GSLB health checks"
```

---

### Phase B: 数据层同步

### Task B1: tenant 表加 home_region 字段

**Files:**
- Create: `scripts/init-db/08-tenant-home-region.sql`
- Modify: `docs/04-data-model.md`

- [ ] **Step 1: 编写 SQL migration**

```sql
ALTER TABLE tenant
  ADD COLUMN IF NOT EXISTS home_region VARCHAR(20) NOT NULL DEFAULT 'sh';

-- 约 1/3 存量租户切到 bj（做地理分散）
UPDATE tenant SET home_region = 'bj'
WHERE id % 3 = 0 AND status = 'active';

CREATE INDEX IF NOT EXISTS idx_tenant_home_region ON tenant(home_region);
```

- [ ] **Step 2: 在两个 Region 都执行 migration**

```bash
psql "$PG_DSN_SH" -f scripts/init-db/08-tenant-home-region.sql
psql "$PG_DSN_BJ" -f scripts/init-db/08-tenant-home-region.sql
```

- [ ] **Step 3: 提交**

```bash
git add scripts/init-db/08-tenant-home-region.sql docs/04-data-model.md
git commit -m "feat(multi-region): add home_region to tenant table"
```

---

### Task B2: PG 逻辑订阅双向配置

**Files:**
- Create: `scripts/multi-region/setup-pg-logical-replication.sh`

- [ ] **Step 1: 编写订阅安装脚本**

```bash
#!/bin/bash
# scripts/multi-region/setup-pg-logical-replication.sh
set -euo pipefail

TENANT_ID=$1
HOME_REGION=$2

if [ "$HOME_REGION" = "sh" ]; then
  PRIMARY_DSN=$PG_DSN_SH; STANDBY_DSN=$PG_DSN_BJ
  PUB_NAME="pub_tenant_${TENANT_ID}_sh"
  SUB_NAME="sub_tenant_${TENANT_ID}_sh"
else
  PRIMARY_DSN=$PG_DSN_BJ; STANDBY_DSN=$PG_DSN_SH
  PUB_NAME="pub_tenant_${TENANT_ID}_bj"
  SUB_NAME="sub_tenant_${TENANT_ID}_bj"
fi

psql "$PRIMARY_DSN" <<SQL
  DROP PUBLICATION IF EXISTS ${PUB_NAME};
  CREATE PUBLICATION ${PUB_NAME} FOR ALL TABLES;
SQL

psql "$STANDBY_DSN" <<SQL
  DROP SUBSCRIPTION IF EXISTS ${SUB_NAME};
  CREATE SUBSCRIPTION ${SUB_NAME}
    CONNECTION '${PRIMARY_DSN}'
    PUBLICATION ${PUB_NAME}
    WITH (origin = none);
SQL

sleep 2
psql "$STANDBY_DSN" -c "SELECT pid, state, replay_lag FROM pg_stat_wal_receiver;"
```

- [ ] **Step 2: 为所有活跃租户执行**

```bash
psql "$PG_DSN_SH" -Atc "SELECT id, home_region FROM tenant WHERE status='active'" \
  | while read -r id region; do
      ./setup-pg-logical-replication.sh "$id" "$region"
    done
```

- [ ] **Step 3: 验证复制**

```bash
psql "$PG_DSN_SH" -c "UPDATE tenant SET name='test_replication' WHERE id=2"
sleep 2
psql "$PG_DSN_BJ" -c "SELECT name FROM tenant WHERE id=2"
# Expected: test_replication

psql "$PG_DSN_BJ" -c "SELECT EXTRACT(epoch FROM replay_lag) AS lag_seconds FROM pg_stat_wal_receiver;"
# Expected: < 1
```

- [ ] **Step 4: 提交**

```bash
git add scripts/multi-region/setup-pg-logical-replication.sh
git commit -m "feat(multi-region): PG logical replication bidirectional"
```

---

### Task B3: Kafka MirrorMaker 部署

**Files:**
- Create: `scripts/multi-region/deploy-mirrormaker.sh`

- [ ] **Step 1: 编写部署脚本**

```bash
#!/bin/bash
set -euo pipefail

KAFKA_SH="kafka-sh-1:9092,kafka-sh-2:9092,kafka-sh-3:9092"
KAFKA_BJ="kafka-bj-1:9092,kafka-bj-2:9092,kafka-bj-3:9092"
TOPICS="api-call-events,task-requests,task-failures,audit-events,billing-events"

# sh → bj
docker run -d --name mirrormaker-sh2bj confluentinc/cp-kafka:latest \
  /usr/bin/kafka-mirror-maker \
  --consumer.config <(echo "bootstrap.servers=$KAFKA_SH;group.id=mirrormaker-sh2bj") \
  --producer.config <(echo "bootstrap.servers=$KAFKA_BJ") \
  --whitelist="$TOPICS"

# bj → sh
docker run -d --name mirrormaker-bj2sh confluentinc/cp-kafka:latest \
  /usr/bin/kafka-mirror-maker \
  --consumer.config <(echo "bootstrap.servers=$KAFKA_BJ;group.id=mirrormaker-bj2sh") \
  --producer.config <(echo "bootstrap.servers=$KAFKA_SH") \
  --whitelist="$TOPICS"
```

- [ ] **Step 2: 验证**

```bash
echo '{"test":1}' | kafka-console-producer --bootstrap-server "$KAFKA_SH" --topic api-call-events
sleep 3
kafka-console-consumer --bootstrap-server "$KAFKA_BJ" --topic api-call-events --from-beginning --max-messages 1
# Expected: 测试消息
```

- [ ] **Step 3: 提交**

```bash
git add scripts/multi-region/deploy-mirrormaker.sh
git commit -m "feat(multi-region): Kafka MirrorMaker bidirectional"
```

---

### Task B4: ClickHouse 双集群 + 跨 Region 查询

**Files:**
- Modify: `services/libs/apihub-core/src/apihub_core/config.py`

- [ ] **Step 1: 在 Region B 部署 ClickHouse**

```sql
-- Region B CH
CREATE TABLE api_call_log_kafka
ENGINE = Kafka('kafka-bj:9092', 'api-call-events', 'ch-writer-bj', 'JSONEachRow');

CREATE MATERIALIZED VIEW api_call_log_consumer TO api_call_log AS
SELECT * FROM api_call_log_kafka;
```

- [ ] **Step 2: 更新 config.py 加跨 Region 配置**

```python
# apihub_core/config.py 新增：
home_region: str = Field(default="sh", alias="HOME_REGION")
peer_region_ch_host: str | None = Field(default=None, alias="PEER_REGION_CH_HOST")
```

- [ ] **Step 3: 验证 CH 双集群**

```bash
clickhouse-client -h ch-sh --query "SELECT count() FROM api_call_log WHERE ts > now()-INTERVAL 1 MINUTE"
clickhouse-client -h ch-bj --query "SELECT count() FROM api_call_log WHERE ts > now()-INTERVAL 1 MINUTE"
```

Expected: 各自有独立数据。

- [ ] **Step 4: 提交**

```bash
git add services/libs/apihub-core/src/apihub_core/config.py
git commit -m "feat(multi-region): ClickHouse dual-cluster config"
```

---

### Task B5: Redis 配额按 Region 拆分

**Files:**
- Modify: `services/go/quota/internal/limiter/redis.go`

- [ ] **Step 1: Go quota 加 region 前缀 + split ratio**

```go
type Limiter struct {
    region     string
    splitRatio float64 // 配额比例
}

func (l *Limiter) rateKey(tenantID, apiID, appID, slot string) string {
    return fmt.Sprintf("t:%s:rate:%s:%s:%s:%s",
        l.region, tenantID, apiID, appID, slot)
}

func (l *Limiter) effectiveQuota(rule *models.QuotaRule) int64 {
    return int64(float64(rule.Limit) * l.splitRatio)
}
```

- [ ] **Step 2: 验证配额准确**

```bash
curl -X POST http://quota-bj:8001/v1/quota/check \
  -H "X-Tenant-Id: 2" -H "X-Api-Id: 42" -H "X-App-Id: 10"
```

Expected: 配额递减（只扣 Region B 的 40% 配额）。

- [ ] **Step 3: 提交**

```bash
git add services/go/quota/internal/limiter/redis.go
git commit -m "feat(multi-region): Redis quota with region prefix + split ratio"
```

---

### Task B6: APISIX 租户亲和插件

**Files:**
- Create: `deploy/apisix/plugins/tenant-affinity.lua`
- Modify: `deploy/k8s/base/apigw/apisix-values.yaml`
- Modify: `services/services/auth/src/auth/routes.py`

- [ ] **Step 1: Auth 服务返回 home_region**

```python
@router.post("/internal/auth/check")
async def auth_check(api_key: str):
    key_data = await verify_api_key(api_key)
    home_region = await get_tenant_home_region(key_data["tenant_id"])
    return {
        **key_data,
        "home_region": home_region,
    }
```

- [ ] **Step 2: APISIX 租户亲和插件**

```lua
-- deploy/apisix/plugins/tenant-affinity.lua
local _M = { version = 0.1, priority = 2000, name = "tenant-affinity" }

_M.schema = {
    type = "object",
    properties = {
        write_methods = {
            type = "array", items = { type = "string" },
            default = { "POST", "PUT", "PATCH", "DELETE" }
        },
    },
}

function _M.rewrite(conf, ctx)
    local consumer = ctx.consumer
    if not consumer or not consumer.home_region then return end

    local home = consumer.home_region
    local curr = os.getenv("HOME_REGION") or "sh"
    if home == curr then return end

    local method = ctx.var.request_method
    for _, m in ipairs(conf.write_methods) do
        if m == method then
            local gw = { sh = "https://api-sh.apihub.com", bj = "https://api-bj.apihub.com" }
            local uri = ctx.var.uri
            if ctx.var.is_args == 1 then uri = uri .. "?" .. ctx.var.args end
            ngx.status = 302
            ngx.header["Location"] = (gw[home] or "") .. uri
            return ngx.exit(302)
        end
    end
end

return _M
```

- [ ] **Step 3: 加载插件**

```yaml
# apisix-values.yaml 追加：
plugins:
  - tenant-affinity
```

- [ ] **Step 4: 验证**

```bash
# 写操作跨 Region → 302
curl -s -o /dev/null -w "%{http_code}" -X POST https://api-bj.apihub.com/v1/apis \
  -H "X-API-Key: <sh_tenant_key>"
# Expected: 302

# GET 就地处理 → 200
curl -s -o /dev/null -w "%{http_code}" https://api-bj.apihub.com/v1/apis \
  -H "X-API-Key: <sh_tenant_key>"
# Expected: 200
```

- [ ] **Step 5: 提交**

```bash
git add deploy/apisix/plugins/tenant-affinity.lua deploy/k8s/base/apigw/ services/services/auth/src/auth/routes.py
git commit -m "feat(multi-region): APISIX tenant-affinity plugin + auth home_region"
```

---

### Phase C: 切换与运营

### Task C1: 故障切换 runbook

**Files:**
- Create: `scripts/multi-region/failover-runbook.sh`

- [ ] **Step 1: 编写切换脚本（含 dry-run）**

```bash
#!/bin/bash
set -euo pipefail
FAILED_REGION=$1; DRY_RUN=${2:-""}

if [ "$FAILED_REGION" = "sh" ]; then
  SURVIVING_REGION="bj"
  SURVIVING_PG_DSN=$PG_DSN_BJ
else
  SURVIVING_REGION="sh"
  SURVIVING_PG_DSN=$PG_DSN_SH
fi

echo "[1/5] Dry-run 检查"
curl -sf http://$SURVIVING_REGION-gw:8001/health/ready || exit 1

echo "[2/5] PG 主提升"
for tid in $(psql "$SURVIVING_PG_DSN" -Atc "SELECT id FROM tenant WHERE home_region='$FAILED_REGION'"); do
  [ "$DRY_RUN" = "--dry-run" ] && continue
  psql "$SURVIVING_PG_DSN" -c "ALTER SUBSCRIPTION sub_tenant_${tid}_${FAILED_REGION} DISABLE;"
  psql "$SURVIVING_PG_DSN" -c "UPDATE tenant SET home_region='$SURVIVING_REGION' WHERE id=$tid;"
done

echo "[3/5] DNS 切流"
if [ "$DRY_RUN" != "--dry-run" ]; then
  aliyun alidns UpdateDomainRecord --RecordId "xxxxx" --RR "api" --Type "A" \
    --Value "<$SURVIVING_REGION-slb-ip>" --TTL 30
fi

echo "[4/5] 验证"
curl -sf https://api.apihub.com/health/ready && echo "OK"
```

- [ ] **Step 2: 提交**

```bash
git add scripts/multi-region/failover-runbook.sh
git commit -m "feat(multi-region): failover runbook script"
```

---

### Task C2: Thanos 跨 Region 监控

**Files:**
- Create: `deploy/k8s/overlays/prod-bj/monitoring/thanos.yaml`
- Modify: `deploy/k8s/base/monitoring/prometheus.yaml`

- [ ] **Step 1: Prometheus remote_write**

```yaml
# deploy/k8s/base/monitoring/prometheus.yaml
remote_write:
  - url: "http://thanos-receive:19291/api/v1/receive"
```

- [ ] **Step 2: Thanos Receiver 部署**

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: thanos-receive
  namespace: apihub-monitoring
spec:
  replicas: 3
  template:
    spec:
      containers:
        - name: thanos
          image: quay.io/thanos/thanos:v0.35.0
          args: ["receive", "--grpc-address=0.0.0.0:10901", "--http-address=0.0.0.0:10902", "--remote-write.address=0.0.0.0:19291", "--tsdb.path=/data", "--label=region=\"global\""]
```

- [ ] **Step 3: 提交**

```bash
git add deploy/k8s/base/monitoring/ deploy/k8s/overlays/prod-bj/monitoring/
git commit -m "feat(multi-region): Thanos cross-region monitoring"
```

---

### Task C3: 跨 Region 告警

**Files:**
- Create: `deploy/k8s/base/monitoring/alert-rules-multi-region.yaml`

- [ ] **Step 1: 告警规则**

```yaml
groups:
  - name: multi-region
    rules:
      - alert: RegionDown
        expr: count(up{job="apisix"} == 1) by (region) < 3
        for: 30s
        labels: { severity: p0 }
        annotations:
          summary: "Region {{ $labels.region }} API网关不可用"
      - alert: PGReplicationLag
        expr: pg_replication_lag_seconds > 30
        for: 10s
        labels: { severity: p1 }
        annotations:
          summary: "PG 复制延迟 > 30s"
      - alert: KafkaConsumerLagHigh
        expr: kafka_consumer_lag > 100000
        for: 30s
        labels: { severity: p1 }
      - alert: DNSHealthCheckFailed
        expr: probe_success{target="api.apihub.com"} == 0
        for: 30s
        labels: { severity: p2 }
```

- [ ] **Step 2: 提交**

```bash
git add deploy/k8s/base/monitoring/alert-rules-multi-region.yaml
git commit -m "feat(multi-region): cross-region alert rules"
```

---

### Task C4: 灰度验证

- [ ] **Step 1: 选 1-2 个低风险外部租户**

```sql
SELECT id, code, name FROM tenant WHERE type = 'external' AND status = 'active' LIMIT 2;
```

- [ ] **Step 2: 切到 bj**

```bash
./scripts/multi-region/setup-pg-logical-replication.sh <tid> bj
UPDATE tenant SET home_region = 'bj' WHERE id = <tid>;
```

- [ ] **Step 3: 验证**

```bash
curl -X POST https://api-bj.apihub.com/v1/apis -H "X-API-Key: <test_key>"
# Expected: 200（home=bj，无 302）

curl -s -o /dev/null -w "%{http_code}" -X POST https://api-sh.apihub.com/v1/apis -H "X-API-Key: <test_key>"
# Expected: 302（非 home_region 写跳转）
```

- [ ] **Step 4: 观察 24h**

---

### Task C5: 全量分配

- [ ] **Step 1: 分批切换**

```bash
for tid in $(psql "$PG_DSN_SH" -Atc "SELECT id FROM tenant WHERE home_region='sh' ORDER BY id LIMIT 10"); do
  psql "$PG_DSN_SH" -c "UPDATE tenant SET home_region='bj' WHERE id=$tid;"
  ./setup-pg-logical-replication.sh "$tid" bj
  echo "Tenant $tid → bj"; sleep 300
done
```

- [ ] **Step 2: 确认分布**

```sql
SELECT home_region, count(*) FROM tenant WHERE status='active' GROUP BY home_region;
```

---

### Task C6: 首次故障切换演练

- [ ] **Step 1: dry-run**

```bash
./scripts/multi-region/failover-runbook.sh sh --dry-run
```

- [ ] **Step 2: 执行切换**

```bash
./scripts/multi-region/failover-runbook.sh sh
```

- [ ] **Step 3: 验证**

```bash
curl -sf https://api.apihub.com/health/ready
psql "$PG_DSN_BJ" -c "SELECT count(*) FROM tenant"
```

- [ ] **Step 4: 回滚**（DNS 切回 + home_region 恢复）

- [ ] **Step 5: 演练总结** → 改进项写入 runbook

---

### Task C7: ADR 文档更新

**Files:**
- Modify: `docs/00-decisions.md`

- [ ] **Step 1: ADR-008 → Superseded**

```markdown
> **⚠️ 状态变更**: ADR-008 已被 [ADR-013](#adr-013-多-region-全双活) Superseded（2026-07-14）。
```

- [ ] **Step 2: 新增 ADR-013**

```markdown
## ADR-013 多 Region 全双活

**Status**: Accepted · **Date**: 2026-07-14 · **supersedes**: ADR-008

**决策**: 租户亲和 + 写分区 + 读双活
- Region: cn-shanghai + cn-beijing
- PG: 逻辑订阅双向，origin=none 防循环
- Redis: 独立 Cluster，配额按比例分配
- Kafka: MirrorMaker 双向
- DNS: 阿里云云解析 GSLB
- 切换: 人工确认 + 半自动 runbook
- 演练: 每季度一次
```

- [ ] **Step 3: 提交**

```bash
git add docs/00-decisions.md
git commit -m "docs: ADR-008 superseded by ADR-013 (multi-region)"
```

---

## 执行顺序

```
A1 ─→ A2 ─→ A3 ─────────────────┐
                  │              │
B1 ─→ B2 ───────→ B6             │
B3 ─→ B4                        │
B5                              │
                                │
C1 ←──── 所有 Phase A+B 完成 ────┘
C2 + C3 + C7 (并行，无依赖)
C4 ─→ C5 ─→ C6
```
