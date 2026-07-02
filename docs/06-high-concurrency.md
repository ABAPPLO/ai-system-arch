# 06 · 高并发设计（Python · 10w QPS）

## 1. 10w QPS 的真实拆解

10w QPS 不是单个 Python 服务承担，而是平台整体吞吐。**关键是分层承担**：

| 层 | 承担工作 | 单节点能力 | 横向扩展 |
|----|---------|-----------|---------|
| APISIX 网关 | 路由 / 限流 / 鉴权 / 日志推 Kafka | 5-8w QPS | 5-10 节点 → 30-50w |
| auth 服务 | API Key 校验（高缓存） | 1w QPS（缓存命中） | 5-15 副本 |
| quota 服务 | Redis 计数 + 决策 | 1-2w QPS | 5-15 副本 |
| dispatcher | HTTP 转发 | 5k-1w QPS | 10-50 副本 |
| Kafka | 写入调用事件 | 10w/s 单 broker | 3-6 broker |
| ClickHouse | 调用日志写入 | 100w+/s | 集群 |
| Redis Cluster | 限流计数 / 缓存 | 10w+ op/s | 6-30 节点 |
| 业务后端 | 业务方负责 | 不在平台范畴 | 业务方扩展 |

**结论**：Python 业务服务实际承担 1k-2w QPS（单服务），通过水平扩展达成整体目标。**10w QPS 主要由 APISIX + Redis + Kafka + ClickHouse 扛，Python 只做业务编排**。

## 2. Python async 工程实践

### 2.1 框架与运行时

| 组件 | 选型 | 配置 |
|------|------|------|
| Web 框架 | FastAPI 0.110+ | `default_response_class=ORJSONResponse` |
| ASGI Server | uvicorn | `loop=uvloop`, `http=httptools` |
| 进程管理 | gunicorn | `-k uvicorn.workers.UvicornWorker` |
| Worker 数 | `2 * CPU + 1` | 容器内充分利用多核 |
| max-requests | 10000 + jitter | 防止内存泄漏累积 |
| graceful-timeout | 30s | 优雅退出 |

**启动命令**：
```bash
gunicorn app:app \
  -k uvicorn.workers.UvicornWorker \
  --workers $((2 * $(nproc) + 1)) \
  --bind 0.0.0.0:8000 \
  --preload \
  --max-requests 10000 \
  --max-requests-jitter 1000 \
  --graceful-timeout 30 \
  --timeout 60 \
  --keep-alive 5 \
  --worker-tmp-dir /dev/shm
```

### 2.2 必须遵循的 async 规则

| 规则 | 原因 |
|------|------|
| 所有路由函数 `async def` | sync def 会跑在线程池，性能下降 |
| 严禁同步 I/O 库（requests、psycopg2） | 阻塞事件循环 |
| 阻塞调用必须 `run_in_executor` | 隔离阻塞 |
| CPU 密集任务丢 worker（Celery） | 不阻塞主循环 |
| 用 asyncpg / motor / aiokafka | 真正 async |
| 用 httpx.AsyncClient（连接池复用） | 不要每次 `httpx.get` |
| 用 orjson 替代 json | 5-10x 速度 |

### 2.3 异步驱动对照表

| 操作 | ❌ 禁用 | ✅ 使用 |
|------|---------|---------|
| PostgreSQL | psycopg2 (sync) | **asyncpg** |
| PostgreSQL ORM | SQLAlchemy sync | **SQLAlchemy 2.0 async** |
| Redis | redis-py sync | **redis.asyncio** |
| Kafka | confluent-kafka | **aiokafka** |
| HTTP 客户端 | requests | **httpx.AsyncClient** |
| MongoDB | pymongo | **motor** |
| 时间 | time.sleep | **asyncio.sleep** |
| 文件 | open() | **aiofiles** |

## 3. 连接池调优

每个 Pod 维持适量连接，通过 Pod 数水平扩展总连接数。

### 3.1 asyncpg 连接池

```python
pool = await asyncpg.create_pool(
    dsn=DSN,
    min_size=10,                            # 空闲时也保持
    max_size=50,                            # 单 Pod 上限
    max_queries=50000,                      # 连接复用 5w 次后重建
    max_inactive_connection_lifetime=300,   # 空闲 5min 关闭
    command_timeout=5,                      # SQL 超时
    timeout=2,                              # 获取连接超时
)
```

**总连接数估算**：
- 50 副本 × 50 连接 = 2500 连接
- PG max_connections 设 5000，留余量
- 用 PgBouncer 进一步降低（transaction 模式）

### 3.2 Redis 连接池

```python
pool = redis.ConnectionPool.from_url(
    "rediss://...",
    max_connections=100,
    decode_responses=False,                 # 直接 bytes，省一次解码
    socket_keepalive=True,
    health_check_interval=30,
    socket_timeout=1,
    socket_connect_timeout=1,
    retry_on_timeout=True,
)
```

### 3.3 httpx 连接池

```python
limits = httpx.Limits(
    max_connections=100,
    max_keepalive_connections=20,
    keepalive_expiry=30,
)
client = httpx.AsyncClient(
    limits=limits,
    timeout=httpx.Timeout(5.0, connect=1.0),
    http2=True,                             # 业务后端支持则开
)
```

### 3.4 Kafka 生产者

```python
producer = AIOKafkaProducer(
    bootstrap_servers=BROKERS,
    value_serializer=lambda v: orjson.dumps(v),
    acks="all",                             # 强一致场景；性能优先用 1
    linger_ms=20,                           # 攒批发送
    compression_type="lz4",                 # 压缩
    max_batch_size=16384,
    enable_idempotence=True,                # 幂等
)
```

## 4. 多级缓存

```
请求 → L1（进程内 LRU）→ L2（Redis Cluster）→ DB
```

| 层 | 工具 | TTL | 容量 | 用途 |
|----|------|-----|------|------|
| L1 | cachetools.TTLCache | 5-30s | 1k-10k | 抗热点，单 Pod |
| L2 | Redis Cluster | 5-10min | 无上限 | 主缓存 |
| DB | PostgreSQL | - | - | 源 |

**缓存对象**：
- 接口元数据（最热）
- API Key 元数据（每次调用都查）
- 应用授权关系
- 限流规则
- 业务字典数据

**失效策略**：
- 主动失效：API 元数据变更时，通过 Redis pub/sub 通知所有 Pod 清 L1
- 被动失效：TTL
- 防穿透：空值缓存 30s + 布隆过滤器（Key 不存在快速返回）

## 5. Kafka 分区与吞吐

### 5.1 topic 设计（含租户分区键 [ADR-009](00-decisions.md#adr-009-多租户策略)）

| topic | 分区数 | 副本 | retention | 说明 |
|-------|-------|------|----------|------|
| api-call-events | 64 | 3 | 7 天 | 调用事件流（最热） |
| task-requests | 32 | 3 | 1 天 | 异步任务派发 |
| task-failures | 16 | 3 | 7 天 | 失败投递 |
| audit-events | 8 | 3 | 30 天 | 审计（等保 6 月+，最终归档 OSS） |
| notification-events | 8 | 3 | 7 天 | 通知（钉钉 / 邮件 / 短信） |
| sdk-build | 4 | 3 | 1 天 | SDK 触发 |
| billing-events | 4 | 3 | 90 天 | Phase 3 计费 |

### 5.2 分区策略

- **api-call-events**：分区键 = `{tenant_id}:{trace_id}` hash → 同租户的事件顺序集中
- **task-requests**：分区键 = `{tenant_id}:{api_id}` hash
- **task-failures**：分区键 = `{tenant_id}:{retry_task_id}` hash
- **audit-events**：分区键 = `{tenant_id}:{actor_id}` hash

### 5.3 消费者组

每个消费组独立 offset，互不干扰：

| 消费组 | 作用 | 并发 |
|--------|------|------|
| ch-writer | Kafka → ClickHouse | 64 consumer（每分区 1 个） |
| retry-handler | 失败 → 延迟队列 | 16 |
| audit-writer | 落 PG 审计表 | 8 |
| realtime-stats | 实时大盘（可选 Flink） | 16 |

## 6. ClickHouse 写入优化

### 6.1 不让 Python 直写 CH

调用事件 → Kafka → ClickHouse Kafka Engine → MergeTree。**Python 不直接连 ClickHouse 写**。

### 6.2 Kafka Engine 直接消费

```sql
CREATE TABLE api_call_log_queue AS api_call_log
  ENGINE = Kafka(
    'kafka:9092',
    'api-call-events',
    'ch-writer',
    'JSONEachRow'
  );

CREATE MATERIALIZED VIEW api_call_log_mv TO api_call_log AS
  SELECT * FROM api_call_log_queue;
```

### 6.3 part 管理要点

- 单次插入 ≥ 1w 行或 ≥ 1MB，避免小 part
- Kafka Engine 自带攒批：`max_block_size=1048576`, `flush_interval_ms=2000`
- 监控 `system.parts` 数量，超过阈值告警

### 6.4 TTL 与冷热分层

```sql
ALTER TABLE api_call_log MODIFY TTL
    ts + INTERVAL 7 DAY  TO VOLUME 'hot',     -- SSD
    ts + INTERVAL 30 DAY TO VOLUME 'warm',    -- HDD
    ts + INTERVAL 180 DAY DELETE;
```

## 7. Redis Cluster 规划

### 7.1 规模估算

- 10w QPS 调用 × 5 次 Redis 访问（鉴权 + 限流 + 元数据）= 50w ops/s
- 单 Redis 节点 10w ops/s
- 需 5-6 主节点（+ 副本），共 10-12 节点
- 阿里云 Redis 集群版按分片规格选型

### 7.2 Key 设计（多租户 [ADR-009](00-decisions.md#adr-009-多租户策略)）

- 所有 key 加租户前缀 `t:{tenant_id}:`
- 限流 key：`t:{tenant_id}:rate:{api_id}:{app_id}:{minute_slot}` → INCR + EXPIRE
- 元数据 key：`t:{tenant_id}:api:{api_uuid}` / `t:{tenant_id}:app:{app_uuid}`
- Key hash 到不同 slot，避免热点（tenant_id 本身就起到打散作用）
- 热点 API：使用本地 LRU 缓存兜底，降低 Redis 压力

### 7.3 Pipeline 与 Lua

- 限流多 key 操作用 Lua 脚本（原子性 + 减少往返）
- 批量查询用 pipeline
- 避免大 Key（单值 < 1MB，集合 < 1w 元素）

## 8. 慢端点与异常保护

### 8.1 超时分级

| 层 | 默认超时 |
|---|---------|
| 网关层（APISIX） | 30s |
| dispatcher | 5s（可按接口覆盖） |
| auth 服务 | 200ms |
| quota 服务 | 100ms |
| 业务后端 | 1-30s（按接口声明） |
| PG SQL | 1-5s |
| Redis | 100ms |
| Kafka 推送 | 500ms（异步非阻塞） |

### 8.2 熔断（circuit breaker）

```python
# 用 aiobreaker 或自研
breaker = CircuitBreaker(
    fail_max=10,                # 连续 10 次失败
    reset_timeout=30,           # 熔断 30s
    success_threshold=3,        # 半开态连续成功 3 次恢复
)
```

熔断维度：
- 按 API + 后端实例
- 错误率 > 10%（5s 滚动窗口）触发

### 8.3 舱壁隔离

- **核心业务**与**普通业务**用独立 dispatcher 副本（按 namespace 或 K8s deployment 分开）
- 不同业务后端用独立 httpx 连接池
- 互不影响

### 8.4 限流降级

- 限流决策走 Redis，Redis 故障时 → 本地限流（保守值）
- 调用日志写 Kafka 失败 → 写本地文件，后台补录

## 9. 自动扩缩容（HPA）

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: dispatcher
spec:
  scaleTargetRef:
    kind: Deployment
    name: dispatcher
  minReplicas: 5
  maxReplicas: 50
  metrics:
  - type: Resource
    resource:
      name: cpu
      target: { type: Utilization, averageUtilization: 60 }
  - type: Resource
    resource:
      name: memory
      target: { type: Utilization, averageUtilization: 70 }
  - type: Pods
    pods:
      metric: { name: http_requests_per_second }
      target: { type: AverageValue, averageValue: "1000" }
  behavior:
    scaleUp:
      stabilizationWindowSeconds: 30
      policies:
        - type: Percent
          value: 100
          periodSeconds: 30
    scaleDown:
      stabilizationWindowSeconds: 300
      policies:
        - type: Percent
          value: 50
          periodSeconds: 60
```

**关键**：
- 扩容快（30s 内翻倍）
- 缩容慢（5min 稳定窗口，避免抖动）

## 10. 热点 key 处理

### 10.1 识别

- Redis Cluster `hotkeys` 命令
- 自定义埋点：采样 Top-N 调用 API / 调用方
- Prometheus 自定义指标

### 10.2 应对

| 热点类型 | 策略 |
|---------|------|
| 单 API 元数据极热 | L1 进程内缓存，TTL 5s |
| 单调用方高频访问 | 客户端配额收紧，独立限流 |
| 单 Key Redis 计数热 | 多副本 key（rate:{api}:{app}:{slot}_0~9），降低单 key 压力 |
| 限流决策热 | 部分决策下移到 APISIX 本地（基于配额下发的本地限流） |

## 11. 压测与验证

### 11.1 工具

- **k6**（推荐）：脚本灵活，分布式压测
- **Locust**：Python 写场景，团队熟悉
- **wrk2**：单机极致性能

### 11.2 压测场景

| 场景 | 目的 | 频率 |
|------|------|------|
| 单接口基线 | 找出单 Pod 容量 | 每次发布 |
| 混合流量（7:2:1 读写） | 模拟真实负载 | 每周 |
| 峰值压测（突发 2x） | 验证扩缩容 | 每月 |
| 长稳压测（持续 2h+） | 验证稳定性 | 上线前 |
| 故障注入（杀 Pod） | 验证韧性 | 每月 |

### 11.3 性能基线

| 接口类型 | P95 目标 | P99 目标 |
|---------|---------|---------|
| 简单读（无业务后端，纯缓存） | 30ms | 80ms |
| 简单同步（含后端 < 100ms） | 100ms | 200ms |
| 复杂同步（含后端 < 500ms） | 500ms | 1s |
| 异步任务提交 | 50ms | 100ms |

### 11.4 监控指标基线

- CPU 利用率 < 70%
- 内存 < 80%
- 网络 < 60% 带宽
- PG 连接数 < 50% 上限
- Redis 命中率 > 95%

## 12. 性能瓶颈定位工具

| 工具 | 场景 | 侵入性 |
|------|------|--------|
| **py-spy** | 生产环境 CPU 火焰图 | 极低（采样） |
| **Pyinstrument** | 开发环境行级耗时 | 中（导入即用） |
| **OpenTelemetry** | 跨服务慢调用定位 | 低（自动埋点） |
| **Sentry** | 异常聚合 | 低 |
| **pprof_http** | Gunicorn worker CPU | 中 |
| **aiohttp debug middleware** | 单请求各阶段耗时 | 开发环境 |

## 13. Python 特殊优化

### 13.1 GC 调优

```python
import gc
gc.set_threshold(700, 10, 10)  # 默认 700/10/10，高负载可调
```

对长连接对象多的服务，可考虑关闭 GC + 定期手动回收。

### 13.2 uvloop 替代 asyncio loop

```python
# uvicorn 启动时
gunicorn ... -k uvicorn.workers.UvicornWorker --loop uvloop
```

性能提升 2-4x。

### 13.3 字典 / dataclass vs Pydantic

- Pydantic v2 性能 ≈ dataclass
- 简单内部数据结构用 dataclass
- 外部输入用 Pydantic（类型校验有价值）

### 13.4 启用 Cython（仅热点）

个别热点函数可用 Cython 编译，性能提升 3-10x。仅做最后手段。

## 14. 可能的 Go 重写热点

**前提**：先用 Python 跑通 + 压测，定位真实瓶颈，不要过早优化。

候选热点服务：
1. **quota 服务** — Redis 计数 + 决策，单次 < 1ms，对延迟极敏感
2. **APISIX 自定义插件** — Lua 写，不属 Python 范畴

如果业务层压测发现 Python 撑不住，再针对性 Go 重写。**默认不动**。

## 15. 容量规划（参考）

| 资源 | 容量 | 说明 |
|------|------|------|
| ACK 节点 | 30-50 台 ECS（8c16g） | Python 业务 + APISIX |
| RDS PG | 8c32g + 1T SSD | 元数据，主备 |
| Redis Cluster | 8 分片 × 4g | 限流计数 |
| Kafka | 6 broker × 4c8g × 2T | 调用事件 |
| ClickHouse | 5 节点（3 shard + 2 replica）× 8c32g × 5T | 调用日志 |
| SLB | 5w qps 规格 | 入口 |
| 带宽 | 1Gbps 起步 | 公网 |

**预估月度成本**（阿里云）：~10-20 万元 RMB（不含人力）。

## 16. 不要做的事

| ❌ 不要 | 原因 |
|---------|------|
| 不要在 Python 里做全局锁 | 多 worker / 多 Pod 锁不住 |
| 不要在 dispatcher 里同步写 ClickHouse | 阻塞主链路 |
| 不要在路由函数里 sleep | 浪费 worker |
| 不要用 Flask（sync 框架） | 与 async 生态冲突 |
| 不要把大 body 全部入库 | 用 MinIO 引用 |
| 不要对非幂等接口自动重试 | 业务数据风险 |
| 不要无限扩容单 Pod | 用多副本横向扩 |
| 不要过早 Go 重写 | 先量化再优化 |
| 不要在查询中漏掉 tenant_id | 应用层 + RLS 兜底，但漏写会被审计告警 |
| 不要在 Redis key 不加 tenant 前缀 | 多租户数据混在一起 |

## 17. AI 流式调用性能预留（[ADR-004](00-decisions.md#adr-004-ai-网关扩展)，Phase 4 实现）

Phase 4 才接入 LLM，但 Phase 1/2 的基础设施要预留：

### 17.1 SSE 流式响应

- 协议：`text/event-stream`
- Python 实现：FastAPI `StreamingResponse` + async generator
- 关键：每 chunk 立即 flush，不能 buffer
- 超时：流式期间单独控制（默认 5min）

```python
from fastapi.responses import StreamingResponse

@app.post("/v1/chat")
async def chat(req: ChatRequest):
    async def stream():
        async for chunk in llm_client.stream(req):
            yield f"data: {chunk.json()}\n\n"
        yield "data: [DONE]\n\n"
    return StreamingResponse(stream(), media_type="text/event-stream")
```

### 17.2 连接管理

- 流式连接数单独限制（与普通 HTTP 不同），避免 SSE 长连接耗尽 worker
- gunicorn worker 数：`workers = 2 * CPU + 1`，每个 worker `worker_connections=10000`
- 单实例并发流：1k-2k（worker_connections 决定）

### 17.3 Token 配额扣减

- 不是按调用次数扣，而是按 token 数扣
- 流式期间不实时扣（性能考虑），结束时按 `token_total` 一次性扣
- Redis Key：`t:{tenant_id}:tokens:{month_slot}` → INCRBY token_total

### 17.4 失败处理

- 流式中断（用户断开 / 网络异常）按已生成 token 计费
- 上游 LLM 失败 → 不重试（LLM 调用通常不幂等，且昂贵）
- 错误通过 SSE event type=error 返回，HTTP 状态仍 200（避免客户端误判）

### 17.5 监控指标

| 指标 | 说明 |
|------|------|
| `apihub_ai_tokens_total` | Counter, labels: tenant_id, model |
| `apihub_ai_stream_duration_seconds` | Histogram |
| `apihub_ai_active_streams` | Gauge |
