# 09 · 部署方案

> 落实 [ADR-001 阿里云](00-decisions.md#adr-001-云厂商)、[ADR-008 单 Region](00-decisions.md#adr-008-多-region-策略)、[ADR-010 等保 2.0 三级](00-decisions.md#adr-010-数据合规)。

## 1. 单云部署（阿里云）

### 1.1 Region 与可用区

- **Region**：cn-shanghai（按主要用户分布选择）
- **可用区**：3 个 AZ，均匀分布
- **跨 Region**：北京 / 张家口 备份（仅数据备份，暂不做双活）

### 1.2 网络架构（等保 2.0 三级加强 [ADR-010](00-decisions.md#adr-010-数据合规)）

```
┌─────────────────────── VPC: 10.0.0.0/16 ───────────────────────────┐
│                                                                     │
│  ┌─ DMZ 子网 10.0.1.0/24（多 AZ）──────────────────────────────┐    │
│  │  SLB (公网入口)  EIP  WAF  DDoS 高防                            │    │
│  └────────────────────────────────────────────────────────────┘    │
│                                                                     │
│  ┌─ App 子网 10.0.10.0/24（多 AZ）─────────────────────────────┐    │
│  │  ACK 集群节点                                                  │    │
│  │  - APISIX DaemonSet                                            │    │
│  │  - Python 业务 Pods（含 tenant-svc / notification-svc）         │    │
│  │  - Argo Workflow Pods                                          │    │
│  │  - Prometheus / Grafana / Jaeger / Loki                        │    │
│  └────────────────────────────────────────────────────────────┘    │
│                                                                     │
│  ┌─ Data 子网 10.0.20.0/24（多 AZ，无公网）────────────────────┐    │
│  │  RDS PostgreSQL（主备跨 AZ + SQL 审计）                         │    │
│  │  Redis 集群版                                                  │    │
│  │  Kafka 实例                                                    │    │
│  │  ClickHouse ECS 集群                                           │    │
│  │  MinIO ECS（或用 OSS 替代）                                     │    │
│  │  数据库审计 + 数据库防火墙                                       │    │
│  └────────────────────────────────────────────────────────────┘    │
│                                                                     │
│  ┌─ Mgmt 子网 10.0.99.0/24（独立，强审计）─────────────────────┐    │
│  │  堡垒机（运维 SSH 入口，全程录像）                                │    │
│  │  VPN 网关                                                       │    │
│  │  云安全中心                                                     │    │
│  │  密钥管理服务 KMS                                                │    │
│  └────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────┘
```

**等保 2.0 三级网络要求**：
- DMZ / App / Data / Mgmt 四子网隔离，安全组严格白名单
- Data 子网无公网出口（仅 NAT 网关白名单）
- 所有运维操作经堡垒机（强制审计 + 双因素）
- 数据库开启 SQL 审计 + 数据库防火墙
- 跨可用区部署主备

### 1.3 托管服务对照

| 自建组件 | 阿里云产品 | 选型 |
|---------|-----------|------|
| K8s | ACK 托管版 | ✅ 托管，省运维 |
| PostgreSQL | RDS PG（企业版） | ✅ 主备 + 跨 AZ |
| Redis | Redis 7.0 集群版 | ✅ 自带集群 |
| Kafka | 消息队列 Kafka 版 | ✅ 兼容开源 |
| 对象存储 | OSS | ✅ 替代自建 MinIO |
| SLB | SLB | ✅ 入口 LB |
| DNS | 云解析 DNS | ✅ |
| CDN | CDN | ✅ 静态资源 |
| WAF | Web 应用防火墙 | ✅ |
| DDoS | DDoS 高防 | ✅（按需） |
| ClickHouse | 无 | ❌ 自建 ECS 集群 |
| Jaeger | 无 | ❌ 自建 |
| Loki | 无 | ❌ 自建 |
| Prometheus | 无 | ❌ 自建（或用阿里云 ARMS） |

### 1.4 资源规划（生产环境，含等保 2.0 三级组件）

| 资源 | 规格 | 数量 | 月成本（估） |
|------|------|------|------------|
| ACK 节点 | ecs.c7.2xlarge (8c16g) | 30 | ¥36,000 |
| ACK Master | 托管 | - | ¥3,000 |
| RDS PG（含 SQL 审计） | pg.x4.large.2c (4c16g) | 主备 | ¥7,000 |
| Redis 集群 | 8 分片 × 2g | - | ¥8,000 |
| Kafka | 6 broker × 4c8g × 2T | - | ¥12,000 |
| ClickHouse ECS | ecs.r7.2xlarge (8c64g) × 5T SSD | 5 | ¥20,000 |
| SLB | slb.s3.large | - | ¥800 |
| OSS | 标准 5T | - | ¥1,200 |
| CDN | 100TB 流量 | - | ¥20,000 |
| WAF | 高级版 | - | ¥2,000 |
| 带宽 / EIP | 1Gbps 共享 | - | ¥5,000 |
| 监控 / 日志 | ARMS / SLS | - | ¥3,000 |
| **堡垒机**（等保要求） | 云堡垒机 CB-100 | - | ¥1,500 |
| **数据库审计**（等保要求） | dbaudit-basic | - | ¥2,000 |
| **云安全中心**（等保要求） | 高级版 | - | ¥2,500 |
| **KMS** | 按量 | - | ¥500 |
| **DDoS 高防**（按需） | newbgp-20g | - | ¥2,000 |
| **合计** | - | - | **~¥128,500/月** |

**预估年度总成本**：~155 万元 RMB（不含人力 / 备份 / 跨 Region）。

## 2. K8s 集群规划

### 2.1 集群拓扑

| 环境 | 集群数 | 节点规格 | 节点数 |
|------|--------|---------|--------|
| dev | 1 | 8c16g | 5 |
| staging | 1 | 8c16g | 10 |
| prod | 1 | 8c16g | 30 |

**prod 单集群，不分多集群**（初期）。后续如有多 AZ 高可用需求，再考虑多集群 + 多活。

### 2.2 命名空间

```
apihub-system          # 平台核心服务（dispatcher / executor / auth ...）
apihub-data            # 数据相关（ClickHouse / Redis 客户端等）
apihub-monitoring      # Prometheus / Grafana / Loki
apihub-ingress         # APISIX
apihub-workflow        # Argo Workflow（业务长任务）
apihub-tenant-{xxx}    # （可选）大客户独立 namespace
```

### 2.3 节点池

按工作负载分节点池：

| 节点池 | 用途 | 污点 / 亲和 |
|--------|------|------------|
| system | APISIX / 监控 / 日志 | system=true:NoSchedule |
| compute | Python 业务服务 | - |
| data | ClickHouse（如自建） | data=true:NoSchedule |
| workflow | Argo Workflow 任务 | workflow=true:NoSchedule |
| gpu（可选） | AI 推理（未来） | gpu=true:NoSchedule |

## 3. 多环境管理

### 3.1 三套环境对照

| 维度 | dev | staging | prod |
|------|-----|---------|------|
| 目的 | 开发自测 | 集成测试 / 验收 | 生产 |
| 数据 | 假数据 / 部分脱敏 | 脱敏后的真实数据 | 真实数据 |
| 规模 | 最小 | ~30% prod | 100% |
| 可用性 | 单副本 | 多副本 | 多副本 + 跨 AZ |
| 域名 | api-dev.apihub.com | api-staging.apihub.com | api.apihub.com |
| K8s 集群 | 独立 | 独立 | 独立 |
| 数据库 | 独立（小规格） | 独立（中规格） | 独立（高规格） |
| Kafka | 独立 | 独立 | 独立 |
| Redis | 独立 | 独立 | 独立 |
| ClickHouse | 独立 | 独立 | 独立 |
| 监控 | 共享 Prometheus | 共享 | 独立（生产） |
| 告警 | 关闭 | 仅 P0 | 全开 |

**严格物理隔离**，不用 namespace 模拟环境。

### 3.2 环境晋升流程

```
代码合并到 main
    ↓
CI 自动构建镜像 → 推 Harbor
    ↓
ArgoCD 检测 Git 仓库变更
    ↓
自动同步到 dev
    ↓
手动触发 → 同步到 staging
    ↓
验收通过 → 同步到 prod（灰度比例）
```

### 3.3 配置差异

通过 Kustomize overlay 管理环境差异：

```
deploy/k8s/
├── base/                    # 公共基础
│   ├── api-registry/
│   ├── dispatcher/
│   └── ...
├── overlays/
│   ├── dev/                 # dev 覆盖
│   │   ├── kustomization.yaml
│   │   ├── replicas-patch.yaml
│   │   └── configmap.yaml
│   ├── staging/
│   └── prod/
```

每个 overlay 覆盖：
- 副本数
- 资源 limit
- 环境变量
- 数据库连接串（从 Sealed Secrets）
- 域名 / 证书

## 4. GitOps（ArgoCD）

### 4.1 仓库结构

```
git@:apihub-platform/apihub-deploy.git
├── terraform/               # IaC（基础设施）
│   ├── modules/
│   ├── envs/
│   │   ├── dev/
│   │   ├── staging/
│   │   └── prod/
│   └── README.md
├── k8s/                     # K8s manifests
│   ├── base/
│   └── overlays/
│       ├── dev/
│       ├── staging/
│       └── prod/
├── argocd/                  # ArgoCD Application 定义
│   ├── dev.yaml
│   ├── staging.yaml
│   └── prod.yaml
├── apisix/                  # APISIX 配置（也 Git 管理）
│   ├── routes/
│   ├── consumers/
│   └── ssl/
└── helm/                    # Helm charts
```

### 4.2 ArgoCD 配置

```yaml
# argocd/prod.yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: apihub-prod
  namespace: argocd
spec:
  source:
    repoURL: git@:apihub-platform/apihub-deploy.git
    targetRevision: main
    path: k8s/overlays/prod
  destination:
    server: https://kubernetes.default.svc
    namespace: apihub-system
  syncPolicy:
    automated:
      prune: false            # prod 不自动 prune，防误删
      selfHeal: false         # prod 不自动 heal，需手动 sync
    syncOptions:
      - CreateNamespace=true
```

dev / staging 用 `automated.prune: true, selfHeal: true` 自动同步。

### 4.3 接口元数据也走 Git

接口定义 YAML 单独仓库（或 monorepo 子目录）：

```
git@:apihub-platform/apihub-apis.git
├── user-service/
│   ├── user-create.yaml
│   ├── user-query.yaml
│   └── ...
├── order-service/
└── payment-service/
```

每个 API 一个 YAML，PR 评审 + CI 校验 + ArgoCD 同步到平台。

## 5. IaC（Terraform）

### 5.1 模块化

```
terraform/
├── modules/
│   ├── vpc/                 # VPC / 子网 / 路由
│   ├── ack/                 # K8s 集群
│   ├── rds/                 # PG
│   ├── redis/               # Redis 集群
│   ├── kafka/               # Kafka
│   ├── oss/                 # 对象存储
│   ├── slb/                 # 负载均衡
│   ├── eip/                 # 弹性 IP
│   └── monitoring/          # 云监控 / SLS
└── envs/
    ├── dev/                 # dev 环境组合
    ├── staging/
    └── prod/
```

### 5.2 状态管理

- 远程 backend：OSS + TableStore（锁）
- 状态按环境独立
- 严禁手动改云资源

### 5.3 变更流程

```
改 Terraform 代码 → terraform plan → PR 评审 → terraform apply
                                                      ↓
                                              云资源创建 / 修改
                                                      ↓
                                              输出写入 Secret
                                                      ↓
                                              ArgoCD 引用部署
```

## 6. CI/CD

### 6.1 代码仓库

```
apihub-platform/
├── apihub-deploy/            # 部署仓库（GitOps）
├── apihub-apis/              # 接口定义
├── apihub-services/          # Python 服务源码（monorepo）
│   ├── services/
│   │   ├── api-registry/
│   │   ├── dispatcher/
│   │   └── ...
│   ├── libs/                 # 共享库
│   ├── pyproject.toml
│   └── README.md
├── apihub-frontend-admin/    # Admin 前端
└── apihub-frontend-portal/   # Portal 前端
```

### 6.2 CI 流水线（Python 服务）

```yaml
# .gitlab-ci.yml
stages:
  - lint
  - test
  - build
  - publish
  - deploy-dev
  - deploy-staging
  - deploy-prod

lint:
  stage: lint
  script:
    - ruff check .
    - black --check .
    - mypy .

test:
  stage: test
  script:
    - pytest --cov --cov-report=xml
  artifacts:
    reports:
      coverage_report:
        coverage_format: cobertura
        path: coverage.xml

build:
  stage: build
  script:
    - docker build -t $CI_REGISTRY/apihub/$SERVICE:$CI_COMMIT_SHA .
    - docker push $CI_REGISTRY/apihub/$SERVICE:$CI_COMMIT_SHA
    - trivy image $CI_REGISTRY/apihub/$SERVICE:$CI_COMMIT_SHA

publish:
  stage: publish
  only: [main]
  script:
    - docker tag ... :latest
    - docker push ... :latest

deploy-dev:
  stage: deploy-dev
  only: [main]
  script:
    - cd apihub-deploy && yq w -i k8s/overlays/dev/$SERVICE/deployment.yaml ...
    - git commit && git push
    # ArgoCD 自动同步

deploy-staging:
  stage: deploy-staging
  only: [tags]
  when: manual
  script: ...

deploy-prod:
  stage: deploy-prod
  only: [tags]
  when: manual
  script: ...
```

### 6.3 灰度发布

```yaml
# k8s/overlays/prod/dispatcher/canary.yaml
apiVersion: argoproj.io/v1alpha1
kind: Rollout
metadata:
  name: dispatcher
spec:
  strategy:
    canary:
      steps:
      - setWeight: 5
      - pause: { duration: 5m }
      - analysis:
          templates:
          - templateName: success-rate
      - setWeight: 25
      - pause: { duration: 10m }
      - setWeight: 50
      - pause: { duration: 10m }
      - setWeight: 100
```

基于 Argo Rollouts，自动分析错误率，异常自动回滚。

## 7. 备份与灾备

### 7.1 备份策略

| 数据 | 频率 | 保留 | 目标 |
|------|------|------|------|
| PG 元数据 | 每日全备 + 实时 binlog | 30 天 | OSS 跨 Region |
| Redis | 每日快照 | 7 天 | OSS |
| Kafka | 副本 + 跨 AZ | - | 内置冗余 |
| ClickHouse | 每周全备 + 副本 | 30 天 | OSS |
| OSS | 版本管理 + 跨 Region 复制 | 永久 | 跨 Region |
| Git | 多副本 | 永久 | GitLab 备份 |

### 7.2 RPO / RTO

| 数据 | RPO | RTO |
|------|-----|-----|
| 元数据（PG） | 0（同步复制） | 5min |
| 调用日志（CH） | < 1min | 30min |
| 异步任务（PG） | 0 | 5min |
| 审计日志 | 0 | 5min |

### 7.3 灾备演练

- 每季度演练一次 PG 主备切换
- 每半年演练一次 Region 切换（如启用）
- 每月恢复测试（从备份恢复到测试环境）

## 8. 平台运维

### 8.1 窗口

- **变更窗口**：周二 / 周四 14:00-16:00
- **封网期**：节假日前 3 天封网
- **紧急变更**：P0/P1 故障处理，事后补流程

### 8.2 值班

- 工作日 9:00-21:00 双值班
- 非工作时间电话 oncall
- 值班表轮换（每周）

### 8.3 SOP

每个高频故障都有 SOP：
- PG 主库挂
- Redis Cluster 节点挂
- Kafka 消费延迟
- ClickHouse part 过多
- 网关 5xx 突增
- 大量调用失败

每个 SOP 包含：现象、定位、处理、复盘、预防。

## 9. 成本优化

| 项 | 优化策略 |
|---|---------|
| ECS | HPA + Spot 实例（无状态服务用 Spot） |
| RDS | 只读实例按需购买 |
| Redis | 冷热分离，热数据小规格 + 冷数据用 PG |
| Kafka | 按用量调整分区 / retention |
| ClickHouse | 冷热分层（SSD + HDD） |
| 带宽 | CDN 回源优化 + 压缩 |
| 监控 | 降采样（30s → 1min） |

预期优化空间 20-30%。

## 10. 与现有系统集成

### 10.1 接入企业 SSO

- 内部用户：钉钉 / 飞书 / LDAP 单点登录
- 角色：基于 RBAC

### 10.2 接入监控告警

- 钉钉 / 飞书群机器人
- PagerDuty（如有海外业务）

### 10.3 接入工单系统

- 接口变更审批 → 飞书审批 / 钉钉审批
- 外部开发者实名审核 → 自建工单
