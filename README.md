# APIHub · API 中台

> 面向企业内外部开发者的统一 API 接入、管理、调度、监控平台。

## 项目定位

| 角色 | 能力 |
|-----|------|
| **接口提供方** | 通过声明式 YAML 或后台 UI 注册接口，自动获得文档、SDK、监控、限流、鉴权 |
| **内部调用方** | 在管理后台查阅 API、申请授权、获取 SDK、查看调用统计 |
| **外部调用方** | 在开发者门户自助注册账号、申请 API Key、查阅文档、在线调试、查看用量 |
| **平台运维** | 全链路监控、失败重试、流量管控、灰度发布、API 全生命周期治理 |

## 核心能力

- **三类任务统一** — 同步 / 异步 / 长时工作流，对外契约一致
- **高并发** — 网关层 10w+ QPS，业务层水平扩展
- **文档自动化** — 基于 JSON Schema 自动生成 curl / Python / JS / Java 调用示例
- **失败可恢复** — 自动指数退避 + 后台手动重试，完整调用链追溯
- **多环境隔离** — dev / staging / prod 物理隔离，GitOps 晋升
- **可观测** — ClickHouse 调用日志 + Jaeger 全链路 + Grafana 大盘
- **企业级安全** — 多种鉴权、敏感字段脱敏、PII 加密、审计日志
- **开发者门户** — 外部开发者自助接入，降低对接成本
- **SDK 自动生成** — 基于 OpenAPI 自动出 Python/Java/Go/JS SDK，发布到内部 Nexus
- **生命周期治理** — `draft → reviewing → published → deprecated → retired` 全流程

## 技术栈一览

| 层 | 选型 |
|----|------|
| 业务语言 | Python 3.11 + FastAPI + async (uvicorn + gunicorn) |
| 网关 | Apache APISIX |
| 元数据 | PostgreSQL (JSONB) |
| 调用日志 | ClickHouse (MergeTree) |
| 消息队列 | Kafka |
| 缓存 / 限流 | Redis Cluster |
| 对象存储 | MinIO（或 OSS） |
| 长时任务 | Argo Workflow |
| 链路追踪 | Jaeger + OpenTelemetry |
| 监控 | Prometheus + Grafana |
| 日志 | Loki |
| 前端 | Vue 3 + TypeScript + Element Plus |
| 编排 | Kubernetes + ArgoCD |
| IaC | Terraform |
| 云 | 单云（参考阿里云） |

## 文档导航

| # | 文档 | 内容 |
|---|------|------|
| **00** | **[架构决策记录](docs/00-decisions.md)** | **12 个 ADR（云、商业化、租户、合规、审批、Key、IM、多 Region、实名、开放时间）** |
| 01 | [整体架构](docs/01-architecture.md) | 分层、设计原则、关键权衡 |
| 02 | [技术选型](docs/02-tech-stack.md) | 详细选型与备选方案对比 |
| 03 | [微服务拆分](docs/03-services.md) | 15 个微服务职责与边界 |
| 04 | [数据模型](docs/04-data-model.md) | PG / ClickHouse / Redis schema（含多租户 + AI 字段） |
| 05 | [核心流程](docs/05-core-flows.md) | 发布 / 调用 / 重试 / 下线时序 |
| 06 | [高并发设计](docs/06-high-concurrency.md) | Python 应对 10w QPS 工程实践 |
| 07 | [开发者门户](docs/07-developer-portal.md) | 自助接入、文档自动化、SDK |
| 08 | [可观测性与安全](docs/08-observability-security.md) | 日志/指标/链路/审计/鉴权 + 等保 2.0 三级 |
| 09 | [部署方案](docs/09-deployment.md) | 单云、K8s、多环境、GitOps |
| 10 | [路线图](docs/10-roadmap.md) | 4 阶段计划、团队分工、里程碑 |
| **11** | **[多租户设计](docs/11-multi-tenant.md)** | **租户模型、隔离方式、配额、跨租户** |
| **12** | **[本地开发指南](docs/12-dev-guide.md)** | **`make dev-up` 一键起 PG/Redis/Kafka/CH/MinIO/Jaeger/Grafana** |
| **13** | **[架构图集](docs/diagrams.md)** | **12 张 Mermaid 图：分层、部署、时序、状态机** |

## 核心决策一览（详见 [00-decisions.md](docs/00-decisions.md)）

| 维度 | 决策 |
|------|------|
| 云厂商 | 阿里云 |
| 商业化 | 内免外收 |
| 接入方式 | UI + YAML 双轨 |
| AI 网关 | 现在预留扩展点 |
| 审批流 | 分级（dev 自助 / staging 简单 / prod 强审批） |
| Key 轮换 | 推荐但不强制 |
| IM 集成 | 钉钉 |
| 多 Region | 单 Region 长期 |
| **多租户** | **平台多租户（所有表带 tenant_id）** |
| 合规 | 等保 2.0 三级 |
| 实名 | 邮箱 + 手机号 |
| 开放时间 | Phase 3 内（~M11） |

## 当前状态

- ✅ 整体架构设计（2026-07-02）
- ✅ 12 个核心决策评审锁定（2026-07-02）
- ⏳ MVP 试点业务确认 + 预算批准
- ⬜ MVP 开发
- ⬜ 上线

## 仓库结构

```
ai-system-arch/
├── README.md
├── Makefile                       # 一键命令（install / test / tf / k8s）
├── docs/                          # 设计文档（13 篇 + 图集）
├── deploy/
│   ├── terraform/                 # IaC：modules/ + envs/{dev,staging,prod}
│   │   ├── modules/{vpc,ack,rds,redis,kafka,oss}/
│   │   └── envs/dev/              # ✅ dev 已配齐
│   ├── k8s/                       # K8s manifests + Kustomize overlays
│   │   ├── base/{namespaces,apigw,shared}/
│   │   ├── services/api-registry/
│   │   └── overlays/{dev,staging,prod}/
│   └── argocd/                    # ArgoCD Application 三环境
├── services/
│   ├── libs/apihub-core/          # ✅ 共享库（tenant/RLS/redis/kafka/otel/auth）
│   └── services/api-registry/     # ✅ 样例服务（FastAPI + Dockerfile）
├── schema/                        # 声明式接口 YAML（含 http/async/ai 三种类型样例）
│   ├── user-service/
│   └── ai-service/
└── scripts/
    └── validate-schema.py         # CI 校验 schema
```

## 快速开始

### 1. 准备开发环境

```bash
make install                       # 安装 apihub-core + api-registry
pip install pyyaml                 # 给 schema 校验脚本用
```

### 2. 本地起一个服务

```bash
# 准备 PG / Redis / Kafka（用 docker-compose 或远程 dev）
export PG_HOST=localhost PG_USER=apihub PG_PASSWORD=xxx
export REDIS_HOST=localhost KAFKA_BROKERS=localhost:9092

make run-registry                  # uvicorn api_registry.main:app --reload
curl http://localhost:8000/health/live
```

### 3. 校验 schema

```bash
python scripts/validate-schema.py  # CI 集成
```

### 4. 部署基础设施（dev）

```bash
# 准备阿里云凭据
export ALICLOUD_ACCESS_KEY=...
export ALICLOUD_SECRET_KEY=...
export TF_VAR_rds_password='<强密码>'

cd deploy/terraform/envs/dev
terraform init
terraform plan
terraform apply                    # 输出 ACK / RDS / Redis / Kafka 连接信息
```

### 5. GitOps 部署服务

```bash
# 假设 ArgoCD 已装好
kubectl apply -f deploy/argocd/dev.yaml
argocd app sync apihub-dev
```

## 联系

- 技术负责人：TBD
- 项目里程碑与团队分工见 [10-roadmap.md](docs/10-roadmap.md)
