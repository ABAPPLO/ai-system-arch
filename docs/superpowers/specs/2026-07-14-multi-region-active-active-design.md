# Phase 4「多 Region 全双活」设计

> 日期：2026-07-14
> 阶段：Phase 4 演进 — 多 Region 全双活
> 关联：ADR-008（升级单 Region 为全双活）、docs/01-architecture.md、docs/09-deployment.md

## 1. Goal

在已有单 Region（cn-shanghai）基础上，增加第二 Region（cn-beijing）实现**租户亲和的全双活架构**：每个租户固定一个写入 Region，另一 Region 提供读取能力。目标 RTO < 30s，跨 Region PG 复制延迟 < 5s。

### 1.1 已有基础

| 组件 | 状态 | 说明 |
|------|------|------|
| ADR-008 | ✅ 当前单 Region | 明确留了升级到多 Region 的路径 |
| Terraform modules | ✅ | vpc/ack/rds/redis/kafka/oss，6 个模块 |
| K8s + ArgoCD | ✅ | dev/staging/prod 三套环境，GitOps |
| APISIX | ✅ | 自定义插件支持，etcd 配置 |
| PG 逻辑复制 | ✅ 已用于 dev/staging 同步 | 可扩展到跨 Region |
| 所有业务服务 | ✅ 无状态 | 水平扩展无阻碍 |
| Kafka topic | ✅ | 6 个 topic，含租户 Header |

### 1.2 本设计做

- **双 Region 基础设施**：cn-shanghai + cn-beijing 各一套完整基础设施
- **租户亲和路由**：每个租户分配 home_region，写入固定，读取就近
- **PG 双向逻辑订阅**：按 tenant 分 publication 单向复制
- **Redis 独立**：每个 Region 独立 Redis Cluster，限流配额按比例拆分
- **Kafka MirrorMaker**：api-call-events 等 topic 双向复制
- **GSLB 全局流量管理**：就近解析 + 故障切换
- **跨 Region 监控**：Thanos 统一查询、跨 Region 告警

### 1.3 非目标

- ClickHouse 跨 Region merge（各 Region 独立 CH，Admin 查询通过 remote() 函数聚合）
- 实时跨 Region 流量自动漂移（初期固定分配 + 人工切换）
- 跨 Region 分布式事务（写入总有确定的主 Region，不存在分布式事务）
- TiDB/CockroachDB 替换 PG（全双活不依赖原生多主数据库）

### 1.4 成功标准

| 指标 | 目标 |
|------|------|
| 跨 Region PG 复制延迟 | P99 < 5s |
| 故障切换 RTO | < 30s（含 DNS 生效 + PG 主提升） |
| 故障切换 RPO | < 5s（PG 逻辑复制接近实时） |
| 写操作额外延迟（跨 Region 路由） | < 10ms（HTTP 302） |
| 读操作额外延迟 | 0（就近处理） |
| 每季度容灾演练 | ≥ 1 次 |

## 2. 架构总览

```
                    ┌──────────────────┐      ┌──────────────────┐
                    │  Region A         │      │  Region B         │
                    │  (cn-shanghai)    │      │  (cn-beijing)     │
                    │                   │      │                   │
                    │  ┌─ GSLB/DNS ─┐   │      │  ┌─ GSLB/DNS ─┐   │
                    │  │ api.apihub │   │      │  │ api.apihub │   │
                    │  │ .com → A   │   │      │  │ .com → B   │   │
                    │  └─────┬─────┘   │      │  └─────┬─────┘   │
                    │        │         │      │        │         │
                    │  ┌─────▼──────┐  │      │  ┌─────▼──────┐  │
                    │  │ APISIX A   │  │      │  │ APISIX B   │  │
                    │  │ (租户亲和)  │  │      │  │ (租户亲和)  │  │
                    │  └──┬─────┬───┘  │      │  └──┬─────┬───┘  │
                    │     │     │      │      │     │     │      │
                    │  ┌──▼──┐ ┌▼───┐  │      │  ┌──▼──┐ ┌▼───┐  │
                    │  │业务 │ │鉴权│  │      │  │业务 │ │鉴权│  │
                    │  │服务  │ │/配额│ │      │  │服务  │ │/配额│ │
                    │  └─────┘ └────┘  │      │  └─────┘ └────┘  │
                    │  ┌────────────┐  │      │  ┌────────────┐  │
                    │  │ PG Primary  │  │      │  │ PG Replica │  │
                    │  │ (tenant_A) │  │      │  │ (tenant_A) │  │
                    │  └────────────┘  │      │  └────────────┘  │
                    │  ┌────────────┐  │      │  ┌────────────┐  │
                    │  │ PG Replica  │  │      │  │ PG Primary │  │
                    │  │ (tenant_B) │  │      │  │ (tenant_B) │  │
                    │  └─────┬──────┘  │      │  └─────┬──────┘  │
                    │        │         │      │        │         │
                    │        │ PG 逻辑订阅 (双向)         │         │
                    │        ├───────────────────────────┘         │
                    │        ▼                                    │
                    │  ┌────────────┐                             │
                    │  │ Redis A    │      ┌────────────┐         │
                    │  │ (tenant_A  │      │ Redis B    │         │
                    │  │  primary)  │      │ (tenant_B  │         │
                    │  └────────────┘      │  primary)  │         │
                    │  ┌────────────┐      └────────────┘         │
                    │  │ Kafka A    │←──── MirrorMaker ──→│ Kafka B│
                    │  └────────────┘                      └───────┘
                    │  ┌────────────┐      ┌────────────┐         │
                    │  │ CH A       │      │ CH B       │         │
                    │  └────────────┘      └────────────┘         │
                    └──────────────────┘      └──────────────────┘
```

### 2.1 租户亲和模型

```
Tenant              home_region     PG primary      PG standby
─────────────────────────────────────────────────────────────
内部业务线 A        cn-shanghai     cn-shanghai     cn-beijing
内部业务线 B        cn-beijing      cn-beijing      cn-shanghai
外部租户 C          cn-shanghai     cn-shanghai     cn-beijing
外部租户 D          cn-beijing      cn-beijing      cn-shanghai
```

- 写入请求到达非 home_region → APISIX 返回 HTTP 302（`Location: home_region 网关`）
- 读取请求就近处理（备库、本地缓存）
- `tenant` 表新增 `home_region VARCHAR(20)` 字段
- 租户创建时分配 home_region（基于地理位置或权重轮询）

### 2.2 出账数据流

```
全局 Billing Job（任意 Region 触发）
→ 本地 CH 查本 Region 数据
→ remote() 函数查对端 Region CH
→ 合并后写入本 Region PG（primary for standard tenant）
→ 跨 Region 复制自动同步到对端
```

## 3. 基础设施

### 3.1 Terraform

新增 `deploy/terraform/envs/prod-bj/`，复用现有 modules：

| Module | Region B 规格 | 与 Region A 差异 |
|--------|--------------|-----------------|
| vpc | 10.1.0.0/16 | CIDR 不同，需 VPC Peering |
| ack | apihub-prod-bj | 独立集群名 |
| rds | pg.x4.large.2c (4c16g) | 主备跨 AZ，逻辑复制开启 |
| redis | 8 分片 × 2g | 独立实例 |
| kafka | 6 broker × 4c8g | 独立实例 |
| oss | apihub-prod-bj | 独立 bucket 名 |

**VPC Peering**：`terraform/modules/vpc` 增加 peering 参数，建立 sh↔bj 内网互通。

### 3.2 K8s + ArgoCD

| 集群 | ArgoCD Application | Kustomize overlay |
|------|-------------------|-------------------|
| apihub-prod-sh | `prod-sh.yaml` | `overlays/prod-sh/` |
| apihub-prod-bj | `prod-bj.yaml` | `overlays/prod-bj/` |

两套 overlay 差异：数据库连接串（各 Region 连本地 PG/Redis/Kafka）、副本数、资源限制。

### 3.3 ConfigMap 差异

每个 Region 的 ConfigMap 需配置：
- `PG_DSN` → 本地 PG（含对端 PG 作为逻辑复制源）
- `REDIS_URL` → 本地 Redis
- `KAFKA_BROKERS` → 本地 Kafka
- `CH_HOST` → 本地 ClickHouse
- `HOME_REGION` → `sh` / `bj`
- `PEER_REGION_PG_DSN` → 对端 PG（仅逻辑订阅需要）
- `PEER_REGION_CH_HOST` → 对端 ClickHouse（Admin 查询用）

## 4. 数据层

### 4.1 PG 逻辑订阅

每个 tenant 一个 publication，只包含该 tenant 的表：

```sql
-- 在 Region A 为 tenant_A 创建 publication
CREATE PUBLICATION pub_tenant_a_sh
  FOR ALL TABLES;

-- 在 Region B 创建 subscription（从 Region A 订阅）
CREATE SUBSCRIPTION sub_tenant_a_sh
  CONNECTION 'host=pg-sh ...'
  PUBLICATION pub_tenant_a_sh
  WITH (origin = none);    -- 🔑 防止循环复制
```

**`origin = none`**：关键参数，确保只复制本地首次写入的数据，不复制从对端订阅来的数据，防止无限循环。

**数据流向**：
```
Tenant A write → Region A PG (primary) → WAL → publication → Region B PG (standby)
Tenant B write → Region B PG (primary) → WAL → publication → Region A PG (standby)
```

**复制延迟监控**：
```sql
SELECT pid, state, replay_lag FROM pg_stat_wal_receiver;
-- replay_lag 人工告警阀值：> 30s
```

### 4.2 Redis 独立

两个 Region 各自独立 Redis Cluster，Key 含 Region 前缀：

| Key 示例 | Region A | Region B |
|---------|----------|----------|
| 限流计数 | `t:{tid}:rate:sh:{api}:{app}:{slot}` | `t:{tid}:rate:bj:{api}:{app}:{slot}` |
| 元数据缓存 | 各自独立 | 各自独立 |
| 分布式锁 | `lock:t:{tid}:...` | `lock:t:{tid}:...` |

**配额拆分**：租户总配额按 Region 比例分配。如 6:4：
- Region A 配额 = total_quota × 60%
- Region B 配额 = total_quota × 40%
- 任一 Region 用尽 → 返回 429（不跨 Region 借调）

**缓存失效**：元数据变更时，通过 Redis pub/sub 通知对端 Region 清缓存（可选优化，初期允许 TTL 自然过期）。

### 4.3 Kafka MirrorMaker

```
Region A Kafka ──→ MirrorMaker ──→ Region B Kafka (topic: api-call-events)
Region B Kafka ──→ MirrorMaker ──→ Region A Kafka (topic: api-call-events)
```

| topic | 复制方向 | 用途 |
|-------|---------|------|
| api-call-events | 双向 | 调用日志 |
| task-requests | 双向 | 异步任务 |
| task-failures | 双向 | 失败投递 |
| audit-events | 双向 | 审计 |
| notification-events | 单向（A→B） | 通知去重 |
| billing-events | 双向 | 计费 |

**ClickHouse 消费**：各 Region 独立消费组（`ch-writer-sh` / `ch-writer-bj`）消费本地 Kafka。**Python 不直写对端 CH**。

### 4.4 ClickHouse 跨 Region 查询

Admin 全局查询走 `remote()` 函数：

```sql
SELECT * FROM remote('ch-bj:9000', 'default', 'api_call_log', 'admin')
UNION ALL
SELECT * FROM api_call_log  -- 本地
```

Billing Job 和 Trace 服务默认查本地 CH，全局汇总时 remote() 补充对端数据。

## 5. 网关与路由

### 5.1 GSLB（阿里云云解析 DNS）

| 域名 | 解析策略 | 健康检查 |
|------|---------|---------|
| `api.apihub.com` | 智能 DNS（按源 IP 就近） | `/health/ready`，间隔 10s |
| `admin.apihub.com` | 主备（主 sh，备 bj） | 同 |
| `portal.apihub.com` | 智能 DNS | 同 |
| `ws.apihub.com` | 智能 DNS | 同 |

**TTL**：30s（故障切换场景可临时调低到 5s）。

### 5.2 APISIX 租户亲和插件

新建自定义 APISIX plugin（Lua），逻辑：

```lua
-- apisix/plugins/tenant-affinity.lua
-- 在 rewrite 阶段执行

local tenant_id = get_tenant_from_consumer(ctx)
if not tenant_id then return end

local home_region = get_home_region(tenant_id)  -- 从 shared dict / Redis 读取
local current_region = os.getenv("HOME_REGION")  -- "sh" or "bj"

-- 写操作需要路由到 home_region
if is_write_method(ctx) and home_region ~= current_region then
    local peer_gateway = get_peer_gateway(home_region)
    return 302, { location = peer_gateway .. ctx.var.request_uri }
end
```

**读/写判断**：
- GET/HEAD/OPTIONS → 读操作，就地处理
- POST/PUT/PATCH/DELETE → 写操作，需要 home_region

**安全降级**：如果无法获取 home_region（对端 Region 故障），允许写操作就地处理。

### 5.3 Auth 服务扩展

`/internal/auth/check` 返回增加 home_region：

```json
{
  "authenticated": true,
  "tenant_id": "t_001",
  "home_region": "sh",
  "app_id": 42,
  "permissions": [...]
}
```

APISIX 将此信息缓存到 consumer 元数据（TTL 10m），减少重复鉴权。

## 6. 故障切换

### 6.1 故障检测

| 症状 | 判定 | 时限 |
|------|------|------|
| `/health/ready` 连续 3 次失败 | Region 部分异常 | 30s |
| 整体 SLB 不可达 | Region 整体故障 | 30s |
| PG 复制延迟 > 30s | 数据同步链路异常 | 30s 告警 |

### 6.2 切换流程（Region A 整体故障）

```
1. 监控告警触发（P0）
2. Operator 确认 → 执行切换

┌─────────────────────────────────────────┐
│ 3. DNS 切流                             │
│    api.apihub.com A → Region B SLB IP    │
│    等待 2x DNS TTL（60s）确保生效        │
├─────────────────────────────────────────┤
│ 4. PG 主提升（tenant_A 的 primary）      │
│    pg-sh (故障) → pg-bj（从→主提升）      │
│    SELECT pg_promote() ON pg-bj          │
│    停止 tenant_A 的 subscription          │
├─────────────────────────────────────────┤
│ 5. Redis：tenant_A 限流 key 从本地重建   │
│    （非持久状态，从零开始计数）            │
├─────────────────────────────────────────┤
│ 6. Kafka：consumer group 位移切到 bj     │
│    MirrorMaker 方向反转（bj→sh 暂停）    │
├─────────────────────────────────────────┤
│ 7. 验证：curl /health/ready → 200        │
│    通知所有内部 team                     │
└─────────────────────────────────────────┘
```

### 6.3 回滚流程（Region A 恢复）

```
1. Region A 基础设施恢复确认
2. PG 反向同步数据（Region B → Region A）
3. PG 主切回 Region A
4. DNS 切回 → 恢复正常模式
5. 观察 30 分钟确认稳定
```

### 6.4 切换安全门禁

- dry_run 脚本：检查 PG 复制延迟 < 5s、Kafka 堆积 < 1000
- 人工确认 → 自动执行（phase-1 到 phase-5 逐步推进）
- 切换日志自动写入 audit_log（合规要求）
- 每季度强制演练

## 7. 监控与可观测性

### 7.1 Prometheus + Thanos

各 Region Prometheus `remote_write` 到统一 Thanos：

| 指标 | labels | 用途 |
|------|--------|------|
| apihub_requests_total | region, service, status | 请求量 |
| apihub_latency_seconds | region, service | 延迟 |
| pg_replication_lag | region, tenant | 复制延迟 |
| kafka_consumer_lag | region, topic | 消费堆积 |

### 7.2 跨 Region 告警规则

| 级别 | 规则 | 渠道 |
|------|------|------|
| P0 | 任一 Region 整体不可用 | 钉钉 @所有人 |
| P1 | PG 复制延迟 > 30s | 钉钉 |
| P1 | Kafka 堆积 > 100k | 钉钉 |
| P2 | 单 Region P99 延迟 > 2x baseline | 钉钉 |
| P2 | DNS 健康检查连续失败 | 钉钉 |
| P3 | Redis 缓存命中率 < 90% | 钉钉通知 |

### 7.3 跨 Region Trace

Jaeger 各 Region 独立实例，B3 propagation header 贯穿。查询时同时查两个 Jaeger 实例合并展示。

## 8. 分阶段计划

### Phase A：基础设施复制（预计 ~2周）

| # | 任务 | 产出 |
|---|------|------|
| A1 | Terraform 新增 `envs/prod-bj/` | VPC + ACK + RDS + Redis + Kafka + OSS 就绪 |
| A2 | VPC Peering 配置（sh↔bj） | 跨 Region 内网互通 |
| A3 | ArgoCD Application `prod-bj.yaml` | GitOps 双集群 |
| A4 | 两 Region 部署 APISIX + 无状态服务 | 服务双活 |
| A5 | DNS GSLB + 健康检查 | 流量入口 |

### Phase B：数据层同步（预计 ~2周）

| # | 任务 | 产出 |
|---|------|------|
| B1 | tenant 表加 `home_region` + 数据迁移 | 租户亲和就绪 |
| B2 | PG 逻辑订阅（双向 publication） | 元数据跨 Region 复制 |
| B3 | Kafka MirrorMaker 部署 | 事件双向流通 |
| B4 | ClickHouse 独立集群 + remote() 查询 | 跨 Region 日志可查 |
| B5 | Redis 配额按 Region 拆分 | 限流准确 |
| B6 | APISIX 租户亲和插件（Lua） | 写入路由正确 |

### Phase C：切换与运营（预计 ~1周）

| # | 任务 | 产出 |
|---|------|------|
| C1 | 故障切换 runbook + 自动脚本 | 可执行操作手册 |
| C2 | 跨 Region Thanos + 告警配置 | 统一监控 |
| C3 | Thanos 统一查询 + 跨 Region 告警 | 统一可观测 |
| C4 | 灰度验证：选 1-2 租户切到 Region B | 验证通过 |
| C5 | 全量：所有租户 home_region 分配完成 | 全双活运行 |
| C6 | 首次故障切换演练 | 验证及格 |

## 9. 风险

| 风险 | 影响 | 对策 |
|------|------|------|
| PG 逻辑订阅复制延迟导致读旧数据 | 一致性 | 写后读场景强制走主，监控延迟 |
| Redis 配额拆分后利用率不均匀 | 配额定不准 | 定期（按周）重新平衡比例 |
| Kafka MirrorMaker 循环/死信 | 消息重复 | event_id 幂等去重 |
| 跨 Region 网络抖动 | PG 复制延迟增大 | 超时设置 10s，无网容忍 |
| DNS 缓存导致切换慢 | 长尾客户端切不过 | 指导调用方 TTL 设置 ≤ 60s |
| 多个 Region 同时故障（极低概率） | 平台不可用 | 数据已备份到 OSS 跨 Region，可恢复 |

## 10. 成本估算

| 组件 | Region A (sh) | Region B (bj) | 增量成本 |
|------|-------------|-------------|---------|
| ACK 节点 | 30 × 8c16g | 15 × 8c16g（初期减半） | ~+¥18,000 |
| RDS PG | 4c16g 主备 | 4c16g 主备 | ~+¥7,000 |
| Redis | 8 分片 × 2g | 8 分片 × 2g | ~+¥8,000 |
| Kafka | 6 broker | 6 broker | ~+¥12,000 |
| ClickHouse | 5 节点 | 3 节点（初期减量） | ~+¥12,000 |
| SLB + EIP | 1 | 1 | ~+¥800 |
| VPC Peering | — | 跨 Region 流量 | ~+¥500 |
| **合计** | ~¥128,500 | **~¥58,300** | **~+¥58,300** |

**增量月成本**：约 ¥58,300（Region B 初期按 ~50% 规模部署，后续按需扩容）。

## 11. ADR 变更

ADR-008（多 Region 策略）状态改为 **Superseded by ADR-013**。

新建 ADR-013：

| 字段 | 值 |
|------|-----|
| 主题 | 多 Region 全双活 |
| 方案 | 租户亲和 + 写分区 + 读双活 |
| Region | cn-shanghai（主） + cn-beijing（备） |
| PG 复制 | 逻辑订阅，双向，按 tenant 拆分 |
| Redis | 独立 Cluster，配额按比例分配 |
| Kafka | MirrorMaker 双向 |
| DNS | 阿里云云解析 GSLB |
| 切换 | 人工确认 + 半自动执行（runbook） |
| 演练 | 每季度一次 |
