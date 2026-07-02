# 架构图集

> 所有图使用 [Mermaid](https://mermaid.js.org/) 语法，可在 GitHub、VS Code、JetBrains、Typora 等工具直接渲染。
>
> 如需转 draw.io：复制对应 mermaid 代码 → draw.io `Extras → Insert → Advanced → Mermaid`。
>
> 如需导出 PNG/SVG：用 [mermaid.live](https://mermaid.live/) 在线渲染。

## 目录

1. [整体架构（分层）](#1-整体架构分层)
2. [部署拓扑（单 Region / 阿里云）](#2-部署拓扑单-region--阿里云)
3. [同步调用时序图](#3-同步调用时序图)
4. [异步任务时序图](#4-异步任务时序图)
5. [AI 流式调用时序图](#5-ai-流式调用时序图)
6. [接口发布流程（分级审批）](#6-接口发布流程分级审批)
7. [失败三道防线（重试）](#7-失败三道防线重试)
8. [API 生命周期状态机](#8-api-生命周期状态机)
9. [多租户上下文传播](#9-多租户上下文传播)
10. [三层配额决策](#10-三层配额决策)
11. [数据流（Kafka → ClickHouse）](#11-数据流kafka--clickhouse)
12. [微服务依赖](#12-微服务依赖)

---

## 1. 整体架构（分层）

```mermaid
graph TB
    %% 调用方
    subgraph Callers["调用方"]
        A1[Web 后台]
        A2[内部业务服务]
        A3[第三方系统]
        A4[IDE 插件]
    end

    %% 接入层
    subgraph Edge["接入层"]
        B1[DNS + CDN]
        B2[WAF + DDoS 高防]
        B3[SLB + Nginx]
        B1 --> B2 --> B3
    end

    %% 网关层
    subgraph GW["网关层"]
        C1[<b>Apache APISIX</b><br/>动态路由 / 鉴权 / 限流<br/>熔断 / 灰度 / 日志推 Kafka<br/>注入 trace_id + tenant_id]
    end

    %% 业务层
    subgraph Biz["业务层（Python / FastAPI）"]
        direction LR
        subgraph Core["核心服务"]
            D1[API Registry]
            D2[Dispatcher]
            D3[Executor]
            D4[Workflow Svc]
            D5[Auth]
            D6[Quota]
            D7[Retry]
            D8[Trace]
        end
        subgraph Support["支撑服务"]
            D9[Docs]
            D10[SDK Gen]
            D11[Admin BFF]
            D12[Portal BFF]
        end
        subgraph Cross["横切服务"]
            D13[Audit]
            D14[Tenant Svc]
            D15[Notification Svc]
        end
    end

    %% 中间件
    subgraph MW["中间件层"]
        E1[(Kafka)]
        E2[(Redis Cluster)]
        E3[(etcd)]
        E4[(MinIO / OSS)]
    end

    %% 存储
    subgraph DB["存储层"]
        F1[(PostgreSQL<br/>元数据 + RLS)]
        F2[(ClickHouse<br/>调用日志)]
        F3[(Jaeger<br/>链路 Span)]
    end

    %% 可观测
    subgraph Obs["可观测性（横向贯穿）"]
        G1[Prometheus + Grafana]
        G2[Loki]
        G3[AlertManager]
    end

    %% 主流量
    A1 & A2 & A3 & A4 --> Edge
    B3 --> C1
    C1 --> Biz
    Core & Support --> MW
    Cross --> MW
    MW --> DB
    Biz -.metrics/logs.-> Obs

    %% 样式
    classDef gateway fill:#FFE4B5,stroke:#FF8C00,stroke-width:2px
    classDef core fill:#E0FFFF,stroke:#008B8B,stroke-width:1.5px
    classDef cross fill:#FFE4E1,stroke:#DC143C,stroke-width:1.5px
    class C1 gateway
    class D1,D2,D3,D4,D5,D6,D7,D8 core
    class D13,D14,D15 cross
```

**关键说明**：
- **APISIX 扛 80% 流量**（路由/限流/鉴权都是 Nginx 级），Python 业务层只做编排
- **横切服务**（tenant/notification/audit）独立部署，所有业务服务都可调用
- **可观测性贯穿所有层**，不在主流量链路上

---

## 2. 部署拓扑（单 Region / 阿里云）

```mermaid
graph TB
    Internet((Internet)) --> CDN[阿里云 CDN]
    CDN --> WAF[WAF + DDoS 高防]
    WAF --> SLB[SLB 公网入口]

    subgraph Region["Region: cn-shanghai"]
        subgraph VPC["VPC: api-platform-prod (10.0.0.0/16)"]
            
            subgraph DMZ["DMZ 子网 (10.0.1.0/24) - 多 AZ"]
                SLB
            end
            
            subgraph App["App 子网 (10.0.10.0/24) - 多 AZ"]
                ACK["<b>ACK 集群</b><br/>• APISIX (DaemonSet)<br/>• Python 业务 Pods<br/>• Argo Workflow Pods<br/>• Prometheus / Grafana<br/>• Jaeger / Loki"]
            end
            
            subgraph Data["Data 子网 (10.0.20.0/24) - 多 AZ - 无公网"]
                PG[("RDS PostgreSQL<br/>主备 + SQL 审计")]
                REDIS[("Redis 集群版<br/>8 分片")]
                KAFKA[("Kafka<br/>6 broker")]
                CH[("ClickHouse ECS 集群<br/>3 shard + 2 replica")]
                OSS[(OSS)]
            end
            
            subgraph Mgmt["Mgmt 子网 (10.0.99.0/24) - 强审计"]
                BASTION[堡垒机<br/>SSH 入口 + 录像]
                VPN[VPN 网关]
                KMS[KMS 密钥管理]
                SEC[云安全中心]
            end
        end
    end

    SLB --> ACK
    ACK --> PG
    ACK --> REDIS
    ACK --> KAFKA
    ACK --> CH
    ACK --> OSS
    KAFKA -.Kafka Engine.-> CH
    
    BASTION -.运维 SSH.-> ACK
    VPN --> BASTION
    
    %% 跨 Region 备份
    PG -.binlog 备份.-> OSS2[(OSS<br/>cn-beijing)]
    OSS -.跨区域复制.-> OSS2

    classDef external fill:#F0E68C,stroke:#BDB76B
    classDef data fill:#E6E6FA,stroke:#9370DB
    classDef mgmt fill:#FFE4E1,stroke:#DC143C
    class SLB,WAF external
    class PG,REDIS,KAFKA,CH,OSS data
    class BASTION,VPN,KMS,SEC mgmt
```

**等保 2.0 三级要点**：
- DMZ / App / Data / Mgmt **四子网隔离**，安全组严格白名单
- Data 子网**无公网出口**（仅 NAT 网关白名单）
- 所有运维经**堡垒机**（强制审计 + 双因素）
- 跨 Region 仅备份（PG binlog + OSS 复制），不做多活

---

## 3. 同步调用时序图

```mermaid
sequenceDiagram
    autonumber
    participant C as 调用方
    participant G as APISIX
    participant A as Auth Service
    participant Q as Quota Service
    participant D as Dispatcher
    participant B as 业务后端
    participant K as Kafka
    participant CH as ClickHouse

    C->>G: POST /v1/users<br/>Authorization: Bearer ak_xxx
    G->>G: 生成 trace_id（如未传）
    G->>A: 校验 API Key
    Note over A: 查 app_api_key → app → tenant
    A-->>G: ✅ app_id, tenant_id, scopes
    
    G->>Q: 检查三层配额<br/>(tenant / app / api)
    Note over Q: Redis Lua 原子操作
    Q-->>G: ✅ 允许
    
    G->>D: 转发请求<br/>Headers: X-Trace-Id, X-Tenant-Id, X-App-Id
    D->>D: 参数校验 (JSON Schema)
    D->>D: 字段转换 (jq)
    D->>B: HTTP/gRPC 调用
    B-->>D: 响应
    D-->>G: 统一格式包装
    
    par 异步日志
        G->>K: 推调用事件<br/>(tenant_id, trace_id, latency, ...)
        K->>CH: Kafka Engine 消费
    and
        G->>Q: 配额计数 INCR
    end
    
    G-->>C: 200 OK<br/>{code, message, data, meta:{trace_id}}
```

**关键性能点**：
- 步骤 4-5：API Key 元数据缓存 10min，命中率 > 99%
- 步骤 6-7：Redis 限流决策 < 1ms
- 步骤 11：异步推 Kafka，**不阻塞主链路**
- 同步调用 P99 目标 < 200ms（不含业务后端）

---

## 4. 异步任务时序图

```mermaid
sequenceDiagram
    autonumber
    participant C as 调用方
    participant G as APISIX
    participant D as Dispatcher
    participant K as Kafka
    participant E as Executor
    participant B as 业务后端
    participant DB as PostgreSQL
    participant N as Notification Svc

    C->>G: POST /v1/report/generate<br/>X-Mode: async
    G->>D: 鉴权 + 配额检查后转发
    D->>DB: 创建 task_instance<br/>status=pending
    D->>K: 投递任务消息<br/>topic=task-requests
    D-->>G: 立即返回 task_id
    G-->>C: 200 OK<br/>{data:{task_id}, meta:{trace_id}}
    
    Note over E: Executor 异步消费
    E->>K: 消费 task-requests
    E->>DB: 更新 status=running
    E->>B: HTTP 调用业务后端
    
    alt 成功
        B-->>E: 结果
        E->>DB: status=succeeded<br/>写入 response_body
        E->>N: 触发 Webhook 通知
        N->>C: POST callback_url<br/>(带 HMAC 签名)
    else 失败
        B-->>E: 错误
        E->>DB: status=failed<br/>写入 error_*
        E->>K: 推 task-failures
        Note over Retry Svc: 进入重试流程<br/>(见 §7)
    end
    
    Note over C: 调用方也可轮询<br/>GET /tasks/{task_id}
```

**关键设计**：
- 调用方**立即拿到 task_id**，不等待处理
- 结果通过 **Webhook 回调**（HMAC 签名验证）或**轮询**获取
- 失败自动进入重试流程

---

## 5. AI 流式调用时序图

```mermaid
sequenceDiagram
    autonumber
    participant C as 调用方
    participant G as APISIX
    participant D as Dispatcher
    participant LLM as LLM Provider<br/>(OpenAI/Anthropic)
    participant K as Kafka
    participant Q as Quota Svc

    C->>G: POST /v1/chat<br/>Accept: text/event-stream<br/>Authorization: Bearer ak_xxx
    G->>G: 鉴权 + 配额检查（含 token 配额预判）
    G->>D: 转发
    D->>D: 识别 backend_type=ai_model<br/>取 LLM API Key（Vault）<br/>拼接 prompt
    
    D->>LLM: 流式调用
    
    loop 流式输出
        LLM-->>D: chunk (token 增量)
        D-->>G: SSE: data: {chunk}
        G-->>C: SSE: data: {chunk}
    end
    
    LLM-->>D: [DONE]
    D->>D: 统计 token_total
    par 异步
        D->>K: 推调用事件<br/>(token_prompt, token_completion, token_total)
    and
        D->>Q: 扣 token 配额<br/>INCRBY t:tid:tokens:month_slot
    end
    D-->>G: SSE: [DONE]
    G-->>C: SSE: [DONE]
    
    Note over C: 流式断开按已生成 token 计费
```

**关键设计**：
- SSE 协议（`text/event-stream`），每个 chunk 立即转发
- 配额按 token 数扣，不是按调用次数
- **流式中断按已生成 token 计费**
- LLM 调用失败**不重试**（昂贵且非幂等）

---

## 6. 接口发布流程（分级审批）

```mermaid
flowchart TD
    Start([提交发布]) --> EnvCheck{目标环境?}
    
    EnvCheck -->|dev| AutoPath
    EnvCheck -->|staging| SimplePath
    EnvCheck -->|prod| StrictPath
    
    subgraph AutoPath["dev 自助发布"]
        A1[接口提供方自助] --> A2[直接应用]
    end
    
    subgraph SimplePath["staging 简单审批"]
        B1[钉钉机器人推送到业务群]
        B2[业务负责人点"同意"]
        B3[审批通过]
        B1 --> B2 --> B3
    end
    
    subgraph StrictPath["prod 强审批"]
        C1[钉钉审批流<br/>多级: 业务负责人 + 平台运维]
        C2[审批通过]
        C1 --> C2
    end
    
    A2 --> Apply
    B3 --> Apply
    C2 --> Apply
    
    Apply[应用变更] --> Reg[api-registry<br/>写元数据 + tenant_id]
    Reg --> Apisix[写 etcd<br/>APISIX 路由]
    Apisix --> Cache[失效 Redis 缓存<br/>t:tid:api:*]
    Cache --> Docs[触发 docs / sdk-gen]
    Docs --> Audit[审计记录<br/>含 actor + auth_method]
    Audit --> Notify[notification-svc<br/>通知受影响调用方]
    
    Notify --> Gray{需要灰度?}
    Gray -->|是| Canary[Argo Rollouts<br/>5% → 25% → 50% → 100%]
    Gray -->|否| Done([完成])
    Canary --> Done

    classDef gateway fill:#FFE4B5,stroke:#FF8C00
    classDef audit fill:#FFE4E1,stroke:#DC143C
    class Apply gateway
    class Audit audit
```

**生效时间目标**：审批通过后 < 10s 路由可用。

**审批流分级**（[ADR-005](00-decisions.md#adr-005-审批流强度)）：
- **dev**：无审批，自助
- **staging**：钉钉群里点同意即可
- **prod**：钉钉审批流（多级），含灰度策略

---

## 7. 失败三道防线（重试）

```mermaid
flowchart TD
    Fail([业务调用失败]) --> L1
    
    subgraph L1["第 1 道：网关自动重试"]
        L1Check{GET / 幂等 POST?}
        L1Check -->|是| L1Try[立即重试 1-2 次<br/>仅针对 5xx / 网络错误]
        L1Check -->|否| L1Skip[跳过]
        L1Try --> L1R{成功?}
        L1Skip --> L2
        L1R -->|是| Success([✅ 成功])
        L1R -->|否| L2
    end
    
    subgraph L2["第 2 道：业务自动重试"]
        L2Check{接口幂等?}
        L2Check -->|是| L2Try[指数退避<br/>1s → 4s → 16s → 64s<br/>最大 3-5 次]
        L2Check -->|否| L2Skip[跳过]
        L2Try --> L2R{成功?}
        L2Skip --> L3
        L2R -->|是| Success
        L2R -->|否| L3
    end
    
    subgraph L3["第 3 道：后台手动重试"]
        L3DB[retry_task 入 PG<br/>status=exhausted]
        L3DLQ[死信队列]
        L3UI[后台 UI 展示<br/>完整错误 + 重试历史]
        
        L3DB --> L3DLQ --> L3UI
        L3UI --> Choice{用户操作}
        Choice -->|手动重试| Manual[retry_no + 1<br/>保留同一 trace_id]
        Choice -->|忽略| Ignore[标记 ignored]
        Manual --> ManualR{成功?}
        ManualR -->|是| Success
        ManualR -->|否| L3DB
    end

    classDef success fill:#90EE90,stroke:#228B22
    classDef fail fill:#FFB6C1,stroke:#DC143C
    classDef warn fill:#FFE4B5,stroke:#FF8C00
    class Success success
    class L3DLQ,Ignore fail
    class L1Try,L2Try warn
```

**幂等性强制**：
- 接口元数据声明 `idempotent: true` 才允许自动重试
- 调用方应携带 `Idempotency-Key`，业务方据此去重

---

## 8. API 生命周期状态机

```mermaid
stateDiagram-v2
    [*] --> draft: 创建
    
    draft --> reviewing: 提交评审
    reviewing --> draft: 退回修改
    reviewing --> published_dev: 评审通过 → 发布 dev
    
    published_dev --> published_staging: dev 验证 → 晋升 staging
    published_staging --> published_prod: staging 验收 → 晋升 prod
    
    published_prod --> published_prod: 版本升级 (灰度)
    
    published_prod --> deprecated: 标记废弃<br/>(通知调用方)
    deprecated --> published_prod: 取消废弃
    
    deprecated --> retired: 废弃期到期 (30/60/90 天)<br/>仍调用的调用方多次提醒
    retired --> [*]: 元数据保留 6 个月备查
    
    note right of published_prod
        env_status: prod
        监控调用方用量
        跨租户可见性检查
    end note
    
    note right of deprecated
        通知所有调用方
        监控仍有调用?
        联系业务方
    end note
    
    note right of retired
        调用返回 410 Gone
        + 提示新版本
    end note
```

**关键**：
- 每个环境独立状态机（`env_status` 字段）
- 跨环境晋升需走对应审批（dev 自助 / staging 简单 / prod 强审批）
- retired 后调用返回 `410 Gone`

---

## 9. 多租户上下文传播

```mermaid
graph LR
    Req([请求进入<br/>带 API Key]) --> Apisix[APISIX<br/>提取 API Key]
    Apisix --> AuthCheck[Auth Service<br/>校验 + 反查]
    AuthCheck --> Resolve["查 app_api_key<br/>→ app → tenant"]
    Resolve --> Inject["注入 HTTP Headers:<br/>X-Tenant-Id, X-App-Id, X-Trace-Id"]
    
    Inject --> S1[业务服务 1]
    Inject --> S2[业务服务 2]
    Inject --> S3[业务服务 N]
    
    S1 & S2 & S3 --> Propagate{传播到}
    
    Propagate --> PG[(PostgreSQL<br/>SET LOCAL<br/>app.current_tenant_id)]
    Propagate --> Redis[(Redis<br/>Key 加 t:tid: 前缀)]
    Propagate --> Kafka[(Kafka<br/>Header: tenant_id)]
    Propagate --> CH[(ClickHouse<br/>行带 tenant_id)]
    Propagate --> Trace[(Jaeger<br/>tag: tenant.id)]
    Propagate --> Log[(Loki 日志<br/>字段: tenant_id)]
    Propagate --> Svc[下游 HTTP 调用<br/>Header 透传]
    
    Propagate -.强制.-> RLS[(RLS 策略<br/>兜底防漏)]
    
    classDef gateway fill:#FFE4B5,stroke:#FF8C00
    classDef storage fill:#E6E6FA,stroke:#9370DB
    class Apisix,AuthCheck gateway
    class PG,Redis,Kafka,CH,Trace,Log,RLS storage
```

**双重隔离**：
- **应用层**：所有查询强制 `WHERE tenant_id = ?`
- **数据库层**：PostgreSQL RLS（Row Level Security）兜底，即使应用层漏写 WHERE，也无法跨租户读

详见 [11-multi-tenant.md](11-multi-tenant.md)。

---

## 10. 三层配额决策

```mermaid
flowchart TB
    Call([API 调用]) --> Lua[Lua 脚本原子检查]
    
    Lua --> T1{租户级<br/>t:tid:rate:tenant:slot<br/>≤ tenant.qps?}
    Lua --> T2{应用级<br/>t:tid:rate:app:app_id:slot<br/>≤ app.qps?}
    Lua --> T3{API 级<br/>t:tid:rate:api:app:slot<br/>≤ api.qps?}
    
    T1 -->|否| Deny
    T2 -->|否| Deny
    T3 -->|否| Deny
    
    T1 -->|是| A1
    T2 -->|是| A2
    T3 -->|是| A3
    
    A1[三层全部 INCR] --> Allow
    A2[三层全部 INCR] --> Allow
    A3[三层全部 INCR] --> Allow
    
    Allow[✅ 允许调用] --> Forward[转发到 dispatcher]
    
    Deny[❌ 429 Too Many Requests<br/>Header: Retry-After] --> Log[记录限流事件<br/>+ 告警]

    classDef allow fill:#90EE90,stroke:#228B22
    classDef deny fill:#FFB6C1,stroke:#DC143C
    class Allow,Forward allow
    class Deny deny
```

**三层配额**（[ADR-009 多租户](00-decisions.md#adr-009-多租户策略)）：
- **租户级**：整个租户的总配额（防止单租户拖垮平台）
- **应用级**：单应用的配额（防止单应用滥用）
- **API 级**：单接口限流（保护接口稳定性）

三者**取最严**，Redis Lua 脚本原子操作（避免 race condition）。

---

## 11. 数据流（Kafka → ClickHouse）

```mermaid
graph LR
    subgraph Producers["数据生产者"]
        P1[APISIX]
        P2[Dispatcher]
        P3[Executor]
        P4[Auth / Quota]
        P5[业务服务]
    end
    
    subgraph Kafka["Kafka 集群 (6 broker)"]
        T1[(api-call-events<br/>64 分区)]
        T2[(task-requests<br/>32 分区)]
        T3[(task-failures<br/>16 分区)]
        T4[(audit-events<br/>8 分区)]
        T5[(notification-events<br/>8 分区)]
        T6[(billing-events<br/>4 分区<br/>Phase 3)]
    end
    
    subgraph Consumers["消费者"]
        C1[ClickHouse Kafka Engine]
        C2[Retry Handler]
        C3[Audit Writer]
        C4[Notification Svc]
        C5[Billing Aggregator<br/>Phase 3]
    end
    
    subgraph Storage["持久化存储"]
        S1[(("ClickHouse<br/>api_call_log<br/>MergeTree"))]
        S2[(("PostgreSQL<br/>audit_log / retry_task"))]
        S3[钉钉/邮件/短信/Webhook]
        S4[(("PostgreSQL<br/>billing_record"))]
    end
    
    P1 & P2 & P4 --> T1
    P2 --> T2
    P3 --> T3
    P1 & P5 --> T4
    P2 --> T5
    P3 --> T6
    
    T1 -->|Kafka Engine<br/>JSONEachRow| C1 --> S1
    T3 --> C2 --> S2
    T4 --> C3 --> S2
    T5 --> C4 --> S3
    T6 --> C5 --> S4
    
    S1 --> MV1[物化视图<br/>api_call_stats_by_tenant]
    S1 --> MV2[物化视图<br/>实时大盘]
    
    classDef producer fill:#E0FFFF,stroke:#008B8B
    classDef consumer fill:#FFE4B5,stroke:#FF8C00
    classDef storage fill:#E6E6FA,stroke:#9370DB
    class P1,P2,P3,P4,P5 producer
    class C1,C2,C3,C4,C5 consumer
    class S1,S2,S3,S4 storage
```

**关键设计**：
- **Python 业务服务不直接写 ClickHouse**，避免阻塞主链路
- ClickHouse Kafka Engine 直接消费，2s 攒批写入
- 调用日志按 tenant_id + api_id 排序，单租户查询秒回
- 物化视图实时聚合（按小时 / 按租户 / 按 API）

---

## 12. 微服务依赖

```mermaid
graph TB
    %% 前端
    FE1[Admin 前端<br/>Vue 3] --> BFF1
    FE2[Portal 前端<br/>Vue 3] --> BFF2
    
    %% BFF
    BFF1[Admin BFF] --> S1 & S5 & S8 & S14 & S13 & S9 & S10
    BFF2[Portal BFF] --> S1 & S5 & S8 & S9 & S14
    
    %% 网关
    GW[APISIX] --> S5 & S6 & S2
    
    %% 核心服务依赖
    S1[api-registry] --> DB[(PG + Redis)]
    S2[Dispatcher] --> S5 & S6 & DB & MQ[(Kafka)] & S7
    S3[Executor] --> DB & MQ & S7
    S4[Workflow Svc] --> ARGO[Argo Workflow] & MINIO[(MinIO)]
    S5[Auth] --> DB & Redis
    S6[Quota] --> Redis & MQ & CH[(ClickHouse)]
    S7[Retry] --> DB & Redis & MQ & S3
    S8[Trace] --> CH & JAEGER[(Jaeger)] & MINIO
    S9[Docs] --> S1 & MINIO
    S10[SDK Gen] --> S1 & MINIO
    S11[Audit] --> DB & MQ
    S12[Tenant Svc] --> DB & Redis
    S13[Notification Svc] --> DINGTALK[钉钉] & MAIL[邮件] & SMS[短信] & MQ
    
    %% 通知事件来源
    S1 -.发布事件.-> S13
    S7 -.重试耗尽.-> S13
    S11 -.审计.-> S13
    
    classDef bff fill:#FFE4B5,stroke:#FF8C00
    classDef cross fill:#FFE4E1,stroke:#DC143C
    classDef external fill:#F0E68C,stroke:#BDB76B
    class BFF1,BFF2 bff
    class S11,S12,S13 cross
    class DINGTALK,MAIL,SMS,ARGO external
```

**核心规则**：
- 横切服务（Audit / Tenant / Notification）可被任何服务调用，但**不调用业务核心服务**（避免循环依赖）
- 业务核心服务依赖横切服务时走 **Kafka 异步事件**（解耦）
- Dispatcher 是**主流量入口**，依赖最重（Auth + Quota + Kafka + Retry）

---

## 渲染工具推荐

| 工具 | 用途 |
|------|------|
| **GitHub** | 直接预览（原生支持 mermaid） |
| **VS Code** | Markdown Preview Enhanced 或 Mermaid 插件 |
| **JetBrains** | 内置支持（2023.1+） |
| **[mermaid.live](https://mermaid.live/)** | 在线渲染 + 导出 PNG/SVG |
| **draw.io** | `Extras → Insert → Advanced → Mermaid` |
| **Typora / Obsidian** | 原生支持 |

## 更新约定

- 修改架构时**必须同步更新本文档**
- 图修改后，对应的文档（01-architecture.md / 03-services.md / 等）也要同步
- PR 评审时检查图与代码是否一致
