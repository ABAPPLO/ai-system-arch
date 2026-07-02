# 02 · 技术选型

## 1. 选型总表

| 层 | 主选 | 备选 | 选型理由 |
|---|------|------|---------|
| API 网关 | **Apache APISIX** | Kong / Higress | 性能、动态配置、中文社区 |
| 业务语言 | **Python 3.11** | Go / Java | 团队技术栈 + AI 生态 |
| Web 框架 | **FastAPI** | Flask / Sanic | async native + 自动 OpenAPI |
| ASGI Server | **uvicorn + gunicorn** | hypercorn | uvloop 性能 + 多进程 |
| 元数据库 | **PostgreSQL 15** | MySQL | JSONB / CTE / 窗口函数 |
| 调用日志 | **ClickHouse** | Doris / ES | 写入吞吐、压缩、聚合 |
| 错误全文检索 | **Elasticsearch**（可选） | Quickwit | 错误堆栈全文搜索 |
| 消息队列 | **Kafka 3.x** | RocketMQ / Pulsar | 吞吐量、生态 |
| 缓存 / 限流 | **Redis Cluster 7** | Redis Sentinel | 水平分片 |
| 对象存储 | **MinIO / OSS** | Ceph | S3 兼容 |
| 长时任务 | **Argo Workflow** | DolphinScheduler / Airflow | K8s 原生、DAG |
| 服务发现 | **etcd + K8s DNS** | Nacos / Consul | APISIX 原生依赖 |
| 链路追踪 | **Jaeger + OpenTelemetry** | SkyWalking / Tempo | OTel 标准 |
| 指标监控 | **Prometheus + Grafana** | VictoriaMetrics | 业界标配 |
| 日志聚合 | **Loki** | ELK | 成本低，与 Grafana 一体 |
| 容器编排 | **Kubernetes** | - | 标配 |
| GitOps | **ArgoCD** | Flux | 业界主流 |
| IaC | **Terraform** | Pulumi | 阿里云支持好 |
| CI / CD | **GitLab CI** | Jenkins / Argo Workflows | 团队习惯 |
| 镜像仓库 | **Harbor** | - | 私有 + 漏洞扫描 |
| 前端框架 | **Vue 3 + TypeScript** | React | 团队栈 |
| 前端 UI | **Element Plus** | Ant Design Vue | 后台风格匹配 |
| 状态管理 | **Pinia** | Vuex | Vue 3 推荐 |
| 构建工具 | **Vite** | Webpack | 速度 |
| API 文档渲染 | **自研 + Stoplight Elements** | Redoc / SwaggerUI | 可控可定制 |
| SDK 生成 | **openapi-generator** | 自研 | 多语言覆盖 |

## 2. Python 生态详细选型

### 2.1 Web 框架：FastAPI

**理由**：
- 基于 Starlette + Pydantic，原生 async
- 自动生成 OpenAPI 3.0，与平台的文档自动化目标天然契合
- 类型注解 → Pydantic schema 校验，零成本
- 性能：实测 QPS 比 Flask 高 3-5x，接近 Go 的 Gin

**避免**：
- Flask：sync 框架，与 async 生态冲突
- Django：太重，自带 ORM 与平台异构数据源不匹配
- Sanic：社区弱，生态差

### 2.2 数据库驱动：asyncpg（非 psycopg2）

```python
import asyncpg

pool = await asyncpg.create_pool(
    dsn="postgresql://...",
    min_size=10,
    max_size=50,
    max_queries=50000,
    max_inactive_connection_lifetime=300,
    command_timeout=5,
)
```

- 比 psycopg2 快 3-5x
- 纯 async，不阻塞事件循环

**ORM 选择**：SQLAlchemy 2.0 async 模式（开发体验 + 性能平衡）。简单查询直接 asyncpg，复杂查询走 ORM。

### 2.3 Redis 客户端：redis.asyncio

```python
from redis.asyncio import Redis, ConnectionPool

pool = ConnectionPool.from_url(
    "rediss://...",
    max_connections=100,
    decode_responses=False,
    socket_keepalive=True,
    health_check_interval=30,
)
```

### 2.4 Kafka 客户端：aiokafka

```python
from aiokafka import AIOKafkaProducer

producer = AIOKafkaProducer(
    bootstrap_servers="kafka:9092",
    value_serializer=lambda v: orjson.dumps(v),
    acks="all",
    linger_ms=20,
    compression_type="lz4",
)
```

**不用 confluent-kafka 的原因**：底层是 C 扩展，在 async 场景需要 `run_in_executor`，多一层开销。

### 2.5 HTTP 客户端：httpx.AsyncClient

- HTTP/1.1 + HTTP/2 都支持
- 连接池内置
- API 与 requests 兼容，迁移成本低

### 2.6 JSON：orjson

```python
from fastapi.responses import ORJSONResponse

app = FastAPI(default_response_class=ORJSONResponse)
```

- 比 stdlib `json` 快 5-10x
- 原生支持 datetime / UUID / dataclass

### 2.7 任务调度（Worker 侧）

短期任务用 **Celery + Redis broker**（仅作为 worker 池），长任务用 Argo Workflow。

**注意**：Celery 不参与核心调用链路，仅做平台内部异步任务（如发送通知、生成 SDK、清理过期数据）。10w QPS 的调用链路完全不经过 Celery。

### 2.8 后台任务调度（更轻量方案）

考虑 **dramatiq** 或 **arq**（基于 Redis）替代 Celery，更轻量，性能更好。后续团队选型。

## 3. 前端选型理由

| 选项 | 理由 |
|------|------|
| Vue 3 + Composition API | 团队栈，文档好 |
| TypeScript | 类型安全，重构友好 |
| Vite | 启动快、HMR 快 |
| Pinia | Vue 3 官方推荐 |
| Element Plus | 后台组件最全 |
| VueUse | 工具 hooks |
| UnoCSS（可选） | 原子化 CSS |

## 4. 大数据组件适配

平台本身不是大数据平台，但调用日志（10w/s 写入）和实时统计需要大数据组件：

| 组件 | 在本平台的角色 |
|------|---------------|
| Kafka | 调用事件流、任务消息 |
| ClickHouse | 调用日志存储 + 实时聚合 |
| Flink（可选，后期） | 实时计算大盘指标（QPS、错误率） |
| Spark（可选） | 离线报表、配额结算 |

**不引入**：Hadoop / HBase / Hive，平台规模不需要重型数仓。

## 5. 阿里云托管服务对照

| 自建组件 | 阿里云托管 | 建议 |
|---------|-----------|------|
| K8s | ACK 托管版 | ✅ 用托管 |
| PostgreSQL | RDS PG | ✅ 用托管 |
| Redis | Redis 集群版 | ✅ 用托管 |
| Kafka | 消息队列 Kafka 版 | ✅ 用托管 |
| 对象存储 | OSS | ✅ 用托管 |
| 日志 | SLS | ⚠️ 部分用，Loki 自建 |
| 监控 | 云监控 | ⚠️ 基础监控用云监控，应用层用 Prometheus |
| ClickHouse | 无 | ❌ 自建 ECS 集群 |
| Jaeger | 无 | ❌ 自建 |

**理由**：托管服务省运维，自建 ClickHouse 因为阿里云没好产品，Jaeger 自建因为可控性强。

## 6. 版本基线（设计阶段）

| 组件 | 版本 |
|------|------|
| Python | 3.11+ |
| FastAPI | 0.110+ |
| PostgreSQL | 15+ |
| ClickHouse | 23.8 LTS+ |
| Kafka | 3.6+ |
| Redis | 7.2+ |
| APISIX | 3.7+ |
| Kubernetes | 1.28+ |
| ArgoCD | 2.10+ |
| Argo Workflow | 3.5+ |
| Vue | 3.4+ |
| Element Plus | 2.6+ |

## 7. 避免的坑

| 坑 | 对策 |
|----|------|
| Python GIL 阻塞 | 全 async，CPU 密集任务丢 executor 或 Celery |
| 同步 DB 驱动 | 强制 asyncpg / motor / aiokafka |
| json 序列化慢 | 强制 orjson |
| 大量小对象 | 用 dataclass / Pydantic，避免手撸 dict |
| 连接数失控 | 每个客户端设上限，监控 PG `max_connections` |
| 日志同步写盘 | Python 业务日志走 stdout，由 Fluentd 收集到 Loki |
| 热点 API 拖垮实例 | HPA + 舱壁隔离 + 熔断 |
| ClickHouse 小 part 过多 | 批量写，避免高频单条插入 |

## 8. 与 AI 生态的预留（[ADR-004](00-decisions.md#adr-004-ai-网关扩展)）

Python 选型的额外好处：未来平台可承接 AI 推理网关、向量检索、LLM 代理等能力，与现有 API 中台复用基础设施。后续可拓展：

- AI 模型注册（OpenAI / Anthropic / 自研模型统一接入）
- 推理请求路由（按成本 / 延迟 / 质量路由）
- 流式响应（SSE / WebSocket，LLM 流式输出）
- Token 计费与配额

这是 Python 的战略价值。**Phase 1/2 仅 schema 预留**（接口元数据 `backend_type=ai_model`、调用日志 `token_*` 字段、配额 `token_quota` 维度），Phase 4 实现。

## 9. 多租户相关组件（[ADR-009](00-decisions.md#adr-009-多租户策略)）

| 组件 | 用途 |
|------|------|
| **PostgreSQL Row Level Security** | 数据库层兜底隔离，防止应用层 bug 导致越权 |
| asyncpg `SET LOCAL app.current_tenant_id` | 每次获取连接时设租户 session 变量 |
| FastAPI Dependency `current_tenant` | 注入租户上下文到每个请求 |
| OpenTelemetry tag `tenant.id` | 链路追踪带租户 |

## 10. 钉钉相关组件（[ADR-007](00-decisions.md#adr-007-im-集成)）

| 组件 | 用途 |
|------|------|
| **dingtalk-sdk-python** | 钉钉开放平台 SDK（审批、消息、通讯录） |
| **dingtalk-stream-sdk-python** | 钉钉事件订阅（Stream 模式，无需公网回调） |
| 阿里云短信 / DirectMail | 短信、邮件通道 |

## 11. 等保 2.0 三级相关组件（[ADR-010](00-decisions.md#adr-010-数据合规)）

| 组件 | 用途 |
|------|------|
| 阿里云 KMS | 密钥管理（敏感字段加密、备份加密） |
| 阿里云堡垒机 | 运维审计、SSH 入口 |
| 阿里云数据库审计 | SQL 审计日志 |
| 阿里云云安全中心 | 主机入侵检测、漏洞管理 |
| 阿里云日志审计 | 集中审计日志归档 |
| Trivy / Safety | 镜像与依赖漏洞扫描 |
