# 03 · 微服务拆分

## 1. 拆分原则

- **按业务能力拆** — 不按技术分层，避免贫血服务
- **读写分离** — 高频读（接口元数据）与高频写（调用日志）独立
- **BFF 隔离** — 内部后台与外部门户各自 BFF，安全边界清晰
- **平台 vs 业务后端** — 平台服务不写业务逻辑，业务后端由业务方提供
- **多租户隔离** — 所有服务必须支持租户上下文（[ADR-009](00-decisions.md#adr-009-多租户策略)），横切服务（tenant-svc / notification-svc / audit）独立部署
- **粒度适中** — 15 个服务对 10+ 人团队合理，过细会增运维成本

## 2. 服务清单

> 共 15 个服务（含 [ADR-009 多租户](00-decisions.md#adr-009-多租户策略) 新增的 tenant-svc 与 [ADR-007 钉钉集成](00-decisions.md#adr-007-im-集成) 新增的 notification-svc）。

```
                              ┌─────────────┐
                              │  前端入口    │
                              │ Admin / Portal│
                              └──────┬──────┘
                                     │
                ┌────────────────────┼────────────────────┐
                │                    │                    │
        ┌───────▼───────┐    ┌──────▼───────┐    ┌───────▼───────┐
        │ Admin BFF     │    │ Portal BFF   │    │ Docs Service  │
        │ (内部)        │    │ (外部)        │    │ (文档/SDK)    │
        └───────┬───────┘    └──────┬───────┘    └───────────────┘
                │                    │
        ┌───────┴───────────────────┴─────────────────────────────┐
        │              平台核心服务                                    │
        ├──────────────────────────────────────────────────────────┤
        │  API Registry  │  Dispatcher  │  Executor    │  Workflow │
        │  Auth          │  Quota       │  Retry       │  Trace    │
        │  Docs          │  SdkGen      │  Audit       │            │
        └──────────────────────────────────────────────────────────┘
                │
        ┌───────┴────────────────────────────────────────┐
        │  横切服务                                         │
        │  Tenant Svc (多租户管理)                          │
        │  Notification Svc (钉钉/邮件/Webhook/站内信)       │
        └──────────────────────────────────────────────────┘
                              │
                ┌─────────────┴─────────────┐
                │  APISIX Gateway           │
                └───────────────────────────┘
```

| # | 服务 | 类型 | 主要用户 | 核心职责 |
|---|------|------|---------|---------|
| 1 | api-registry | 核心 | 后台 | 接口元数据 CRUD、版本管理、生命周期 |
| 2 | dispatcher | 核心 | 网关 → 业务 | 同步请求路由、参数校验、转换、转发 |
| 3 | executor | 核心 | dispatcher | 异步任务执行、超时控制、结果回写 |
| 4 | workflow-svc | 核心 | dispatcher | 长时 DAG 任务调度（封装 Argo） |
| 5 | auth | 核心 | 网关 | 鉴权（JWT/APIKey/签名）、应用管理 |
| 6 | quota | 核心 | 网关 | 配额计数（含租户/应用/API 三层）、限流决策 |
| 7 | retry | 核心 | executor | 失败重试、补偿、死信处理 |
| 8 | trace | 核心 | 后台 / Portal | 调用日志查询、按租户聚合分析 |
| 9 | docs | 业务支撑 | 后台 / Portal | 文档渲染、curl/Python 生成、在线调试 |
| 10 | sdk-gen | 业务支撑 | CI / Portal | OpenAPI → 多语言 SDK |
| 11 | admin-bff | 前端支撑 | Admin 前端 | 内部后台聚合 API |
| 12 | portal-bff | 前端支撑 | Portal 前端 | 外部门户聚合 API |
| 13 | audit | 横切 | 全部 | 审计日志（等保三级要求）、操作记录 |
| **14** | **tenant-svc** | **横切** | **后台 / 全部** | **租户 CRUD、成员管理、配额规则（[ADR-009](00-decisions.md#adr-009-多租户策略)）** |
| **15** | **notification-svc** | **横切** | **全部** | **钉钉审批/通知、邮件、Webhook、站内信（[ADR-007](00-decisions.md#adr-007-im-集成)）** |

## 3. 各服务详细职责

### 3.1 api-registry（接口注册中心）
**职责**：管理接口元数据的全生命周期。

**主要 API**：
```
POST   /admin/apis                  创建接口定义
GET    /admin/apis                  列表（支持过滤）
GET    /admin/apis/{id}             详情
PUT    /admin/apis/{id}             更新
POST   /admin/apis/{id}/publish     发布到某环境
POST   /admin/apis/{id}/deprecate   标记废弃
POST   /admin/apis/{id}/retire      下线
GET    /admin/apis/{id}/versions    版本列表
POST   /admin/apis/{id}/rollback    回滚到版本
```

**依赖**：PostgreSQL、Redis（缓存）、APISIX Admin API（路由下发）

**部署特征**：3-10 副本，HPA。

---

### 3.2 dispatcher（调度分发器）
**职责**：接收网关转发来的同步请求，按接口定义路由到业务后端，做参数转换、超时控制。

**关键点**：
- 不持久化业务数据
- 状态全部在 Redis（接口元数据缓存）
- 长连接复用：对业务后端的 httpx.AsyncClient 连接池

**依赖**：Redis（元数据缓存）、httpx（调用后端）、Kafka（推调用事件）

**部署特征**：10-50 副本，平台最大规模服务，HPA 基于 QPS。

---

### 3.3 executor（任务执行器）
**职责**：执行异步短时任务（秒~分钟级）。

**工作流**：
1. 从 Kafka topic `task-requests` 消费任务
2. 调用业务后端（HTTP / gRPC / MQ）
3. 结果回写 PostgreSQL `task_instance` 表
4. 通过 Webhook 通知调用方（可选）
5. 失败 → 调 retry 服务

**依赖**：Kafka、PostgreSQL、httpx、retry（gRPC）

**部署特征**：5-30 副本，按队列消费组分摊，可按业务线拆分消费组。

---

### 3.4 workflow-svc（工作流服务）
**职责**：管理长时 DAG 任务（分钟~小时级），底层封装 Argo Workflow。

**主要 API**：
```
POST   /workflows                  提交工作流
GET    /workflows/{id}             查询状态
POST   /workflows/{id}/cancel      取消
POST   /workflows/{id}/resume      恢复
GET    /workflows/{id}/steps       步骤详情
GET    /workflows/{id}/logs        日志
```

**依赖**：Argo Workflow（K8s CRD）、MinIO（日志 / 产物）

**部署特征**：3-5 副本，本身轻量；计算资源在 Argo 的 pod 中。

---

### 3.5 auth（鉴权服务）
**职责**：应用（Caller App）管理、API Key 管理、鉴权决策。

**主要 API**：
```
POST   /admin/apps                 创建应用（生成 app_id/app_secret）
GET    /admin/apps/{id}/keys       API Key 列表
POST   /admin/apps/{id}/keys       生成新 API Key
DELETE /admin/apps/{id}/keys/{kid} 吊销 Key
POST   /internal/auth/check        网关调用：校验请求合法性
```

**鉴权方式**：
| 方式 | 适用 |
|------|------|
| JWT | 内部服务调用 |
| APIKey | 内部 + 外部 |
| HMAC 签名 | 高安全场景（金融、合作方） |
| OAuth2 | 第三方代用户调用 |

**依赖**：PostgreSQL、Redis（Key 元数据缓存）

---

### 3.6 quota（配额与计费服务）
**职责**：实时统计调用次数，决策是否超额，生成计费数据。

**关键设计**：
- 限流决策走 Redis Cluster（INCR + EXPIRE），P99 < 1ms
- 配额规则多维度：调用方 × API × 时间窗
- 实时数据写 Kafka → ClickHouse（离线统计 / 计费）
- 计费规则：免费 / 计次 / 包月 / 阶梯（后续扩展）

**主要 API**：
```
POST   /internal/quota/check       网关调用：检查是否超额
POST   /internal/quota/record      记录一次调用
GET    /admin/quota/usage          查询用量
GET    /admin/quota/billing        查询账单（外部开发者）
```

**依赖**：Redis Cluster、Kafka、ClickHouse

**部署特征**：5-15 副本，HPA。**这是 Python 性能最敏感的服务，可后续改 Go**。

---

### 3.7 retry（重试服务）
**职责**：失败调用的自动 / 手动重试。

**机制**：
- 自动重试：消费 Kafka `task-failures`，按策略（指数退避）写入 Redis ZSet 延迟队列
- 延迟队列 worker 轮询到期的任务，重新调用 executor
- 超过最大重试次数 → 入 PostgreSQL `retry_dead_letter`
- 后台手动重试：调用 `/admin/retry/{id}/trigger`

**主要 API**：
```
GET    /admin/retry/failed         失败列表
GET    /admin/retry/{id}           详情（含每次重试历史）
POST   /admin/retry/{id}/trigger   手动重试
POST   /admin/retry/{id}/ignore    标记忽略
GET    /admin/retry/stats          重试统计
```

**依赖**：Kafka、Redis（ZSet 延迟队列）、PostgreSQL、executor

---

### 3.8 trace（调用追踪服务）
**职责**：调用日志查询、聚合分析。

**主要 API**：
```
GET    /admin/calls                调用列表（支持过滤）
GET    /admin/calls/{trace_id}     单次调用详情（含 span）
GET    /admin/calls/stats          聚合统计（按 API / 时间 / 状态）
GET    /admin/calls/export         导出（限 100w 行）
POST   /admin/calls/compare        对比（成功 vs 失败）
```

**关键**：
- 查询走 ClickHouse
- trace_id 关联 Jaeger span
- 错误堆栈走 MinIO（按 trace_id 索引）

**依赖**：ClickHouse、Jaeger、MinIO

---

### 3.9 docs（文档生成服务）
**职责**：基于接口元数据 + JSON Schema 自动生成文档。

**输出**：
- OpenAPI 3.0 spec（YAML / JSON）
- curl / Python / JS / Java 调用样例
- 参数表格（字段名 / 类型 / 必填 / 默认 / 枚举 / 说明）
- 响应示例（成功 + 各错误码）
- 在线调试页面数据

**实现**：基于 `openapi-core` + 自研渲染逻辑。

**主要 API**：
```
GET    /apis/{api_id}/openapi.yaml OpenAPI spec
GET    /apis/{api_id}/docs         渲染后文档 HTML 片段
GET    /apis/{api_id}/examples     多语言示例
POST   /apis/{api_id}/try          在线调试（dev 环境）
```

---

### 3.10 sdk-gen（SDK 生成服务）
**职责**：基于 OpenAPI 自动生成多语言 SDK，发布到内部 Nexus。

**支持语言**：Python、Java、Go、JavaScript、TypeScript、PHP、C#。

**实现**：封装 `openapi-generator`，加上平台统一鉴权 / 错误处理 / 日志埋点的 wrapper。

**触发**：
- 接口发布时自动触发（生成对应版本 SDK）
- 手动触发（CI / 后台）

**主要 API**：
```
POST   /admin/sdk/gen              触发生成
GET    /admin/sdk/list             SDK 列表
GET    /sdk/{api_id}/{lang}/{ver}  下载
```

---

### 3.11 admin-bff（后台 BFF）
**职责**：聚合下游服务，为后台前端提供统一接口。

**特点**：
- 复杂查询、跨表 join 在 BFF 完成
- 权限校验（基于 RBAC）
- 数据脱敏（如 API Key 部分隐藏）

---

### 3.12 portal-bff（门户 BFF）
**职责**：聚合下游服务，为外部开发者门户提供接口。

**特点**：
- 仅暴露开发者自助功能（注册、查文档、调接口、看用量）
- 严格限流（每秒每 IP）
- 弱依赖内部服务（即使内部慢，门户核心功能不受影响）

---

### 3.13 audit（审计服务）
**职责**：记录所有平台配置变更、敏感操作。

**记录内容**：
- 谁（user / app）
- 何时
- 做了什么（创建 / 修改 / 删除 / 发布 / 下线）
- 影响（接口 ID、调用方 ID 等）
- 来源 IP

**存储**：PostgreSQL `audit_log`（短期）+ OSS（冷归档）。**等保 2.0 三级要求在线 ≥ 6 个月**。

---

### 3.14 tenant-svc（租户管理服务）

**职责**：管理租户生命周期、成员关系、配额规则。落实 [ADR-009 平台多租户](00-decisions.md#adr-009-多租户策略)。

**主要 API**：
```
POST   /admin/tenants                  创建租户（仅超管）
GET    /admin/tenants                  列表（按权限过滤可见租户）
GET    /admin/tenants/{id}             详情
PUT    /admin/tenants/{id}             更新
POST   /admin/tenants/{id}/suspend     暂停
POST   /admin/tenants/{id}/resume      恢复
POST   /admin/tenants/{id}/close       关闭

GET    /admin/tenants/{id}/members     成员列表
POST   /admin/tenants/{id}/members     添加成员
DELETE /admin/tenants/{id}/members/{user_id}
PUT    /admin/tenants/{id}/members/{user_id}  改角色

GET    /admin/tenants/{id}/quota       查配额
PUT    /admin/tenants/{id}/quota       改配额（仅超管或租户 owner）
GET    /admin/tenants/{id}/usage       查用量

GET    /admin/tenants/{id}/children    子租户列表（如有层级）
POST   /admin/tenants/{id}/switch      切换当前租户（前端用）
```

**关键点**：
- 所有调用都返回租户上下文（前端用于切换显示）
- 配额变更触发 Redis 缓存失效
- 暂停 / 关闭操作 → 触发通知给租户所有 owner
- 跨租户操作（仅超管）必须审计

**依赖**：PostgreSQL、Redis（缓存租户元数据）、notification-svc（事件通知）、audit（操作记录）

**部署特征**：3-5 副本。

---

### 3.15 notification-svc（通知服务）

**职责**：统一封装多渠道通知能力。落实 [ADR-007 钉钉集成](00-decisions.md#adr-007-im-集成)。

**支持的渠道**：

| 渠道 | 用途 | 实现 |
|------|------|------|
| **钉钉** | 审批流、群机器人告警、工作通知 | 钉钉开放平台 SDK |
| 邮件 | 注册验证、配额预警、变更通知 | 阿里云 DirectMail |
| 短信 | 关键告警、Strong Auth | 阿里云短信服务 |
| Webhook | 异步任务回调、第三方集成 | httpx.AsyncClient |
| 站内信 | Portal / Admin 用户消息 | PG 存储 + WebSocket 推送 |

**主要 API**：
```
POST   /internal/notify/send           发送通知（业务服务调用）
POST   /internal/notify/batch          批量发送
GET    /admin/notifications            查询（按租户过滤）
PUT    /admin/notifications/{id}/read  标记已读

POST   /admin/notify/templates         模板管理
GET    /admin/notify/templates
```

**钉钉审批集成**：
- 接口发布 / 授权申请 → 创建钉钉审批流
- 审批状态回调 → notification-svc 接收 → 更新 api_change_request
- 审批通过 → 触发 api-registry 实际发布

**关键点**：
- 抽象 `Channel` 接口，未来可扩展飞书 / 企业微信（不破坏业务服务）
- 模板化消息（变量插值）
- 失败重试（指数退避，最多 5 次）
- 限流（避免某租户暴增通知量）
- 审计全部通知发送

**依赖**：钉钉开放平台、阿里云 DirectMail、阿里云短信、PG、Kafka（消费通知事件）、audit

**部署特征**：3-5 副本。

---

## 4. 服务通信矩阵

| 调用方 → 被调用方 | 协议 | 说明 |
|------------------|------|------|
| 网关 → dispatcher | HTTP | 同步调用主链路 |
| 网关 → auth | HTTP | 鉴权（高缓存） |
| 网关 → quota | HTTP | 限流决策（极快） |
| dispatcher → executor | Kafka | 异步任务投递 |
| dispatcher → workflow-svc | gRPC | 长时任务 |
| executor → retry | Kafka | 失败投递 |
| retry → executor | HTTP | 重试触发 |
| Admin → 各服务 | HTTP | 后台管理 |
| 业务服务 → trace | ClickHouse 直查 | 不走服务，直查 DB |

## 5. 服务规模估算（生产环境）

| 服务 | CPU / 实例 | 内存 / 实例 | 副本数 | 备注 |
|------|-----------|------------|--------|------|
| api-registry | 1 core | 1G | 3-10 | HPA |
| dispatcher | 2 core | 2G | 10-50 | 主流量 |
| executor | 2 core | 2G | 5-30 | HPA |
| workflow-svc | 1 core | 1G | 3-5 | |
| auth | 1 core | 1G | 5-10 | 高 QPS |
| quota | 2 core | 2G | 5-15 | 性能敏感 |
| retry | 1 core | 1G | 3-10 | |
| trace | 1 core | 2G | 3-8 | |
| docs | 0.5 core | 0.5G | 3-5 | |
| sdk-gen | 2 core | 4G | 2-5 | CPU 密集 |
| admin-bff | 1 core | 1G | 3-5 | |
| portal-bff | 1 core | 1G | 5-10 | 对外 |
| audit | 0.5 core | 0.5G | 3 | |
| **tenant-svc** | **1 core** | **1G** | **3-5** | **高 QPS（每次调用都查租户）** |
| **notification-svc** | **0.5 core** | **1G** | **3-5** | **钉钉 / 邮件 / 短信 / Webhook** |

**预估总资源**：~200-500 核 CPU，~300-600G 内存（不含数据存储）。

## 6. 服务的演进路径

初期可以合并一些服务减少复杂度：

- **MVP 阶段**：admin-bff / portal-bff 合并到 api-registry；audit 合并到 admin-bff
- **成长期**：拆出独立服务
- **成熟期**：dispatcher 可能按业务线拆分（如 pay-dispatcher、ai-dispatcher）

不要一开始就拆 15 个，按 MVP 阶段建议先 7-8 个（详见 [10-roadmap.md](10-roadmap.md)）。多租户相关的 tenant-svc 在 Phase 1 中后期必须就位，否则后续改造工作量大。
