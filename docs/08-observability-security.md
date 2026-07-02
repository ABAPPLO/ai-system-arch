# 08 · 可观测性与安全

> 落实 [ADR-010 等保 2.0 三级](00-decisions.md#adr-010-数据合规)、[ADR-009 多租户](00-decisions.md#adr-009-多租户策略)、[ADR-004 AI 网关预留](00-decisions.md#adr-004-ai-网关扩展)。

## 1. 等保 2.0 三级合规清单

| 类别 | 要求 | 实现 |
|------|------|------|
| **物理与网络安全** | 安全边界、访问控制 | VPC isolation、WAF、安全组 |
| **身份鉴别** | 双因素、密码策略 | 关键账号强制 MFA；密码 ≥ 12 位 + 复杂度 |
| **访问控制** | 最小权限、RBAC | 平台 RBAC（owner/admin/developer/viewer）+ K8s RBAC |
| **安全审计** | 详细审计、保留 ≥ 6 月 | audit_log 表 + OSS 归档，见 §8 审计 |
| **入侵防范** | 漏洞扫描、IDS | 阿里云云安全中心、镜像漏洞扫描 |
| **数据完整性** | 校验、防篡改 | PG binlog + 备份校验、调用日志不可变 |
| **数据保密性** | 传输 + 存储加密 | TLS 1.3 + KMS 字段级加密 |
| **数据备份恢复** | 定期备份 + 演练 | 见 [09-deployment.md §7](09-deployment.md#7-备份与灾备) |
| **剩余信息保护** | 数据销毁可验证 | 用户删除时软删 → 30 天后硬删 |
| **个人信息保护** | PII 加密 + 最小化 | 见 §6.7 |
| **可用性** | 容灾、SLA | 多 AZ + 备份 + RTO < 30min |

等保 2.0 三级要求**审计日志保留 ≥ 180 天**（在线 + 归档），平台目标：在线 6 月 + OSS 永久归档。

## 2. 可观测性三支柱

| 支柱 | 数据 | 工具 | 用途 |
|------|------|------|------|
| 日志（Logs） | 调用日志、应用日志、错误堆栈 | ClickHouse + Loki + MinIO | 排障、追溯 |
| 指标（Metrics） | QPS、延迟、错误率、资源 | Prometheus + Grafana | 监控、告警 |
| 链路（Traces） | 跨服务调用链 | Jaeger + OpenTelemetry | 慢调用定位、依赖分析 |
| 事件（Events） | 接口发布、配额变更、Key 吊销 | PostgreSQL audit_log | 审计、合规 |

补充：
- **Profiling**（py-spy）：CPU 火焰图
- **Real User Monitoring**（前端）：可选 Sentry

## 3. 日志架构

### 3.1 日志分类

| 类型 | 来源 | 存储 | 保留 |
|------|------|------|------|
| 调用日志 | 网关 / dispatcher | ClickHouse | 180 天 |
| 应用日志 | Python 服务 | Loki | 30 天 |
| 错误堆栈 | 各服务异常 | MinIO（按 trace_id 索引） | 180 天 |
| 审计日志 | 后台操作 | PostgreSQL + OSS | 永久 |
| K8s 日志 | Pod / 节点 | Loki | 14 天 |
| 系统日志 | 主机 / 中间件 | 阿里云 SLS | 30 天 |

### 3.2 调用日志链路

```
[调用发生]
     │
     ├─ APISIX 推 Kafka（基础调用事件）
     │
     ├─ dispatcher 推 Kafka（业务后端调用细节）
     │
     ├─ auth / quota 推 Kafka（鉴权 / 限流决策）
     │
     ▼
[Kafka topic: api-call-events]
     │
     ├─→ ClickHouse Kafka Engine → MergeTree
     │
     ├─→ 实时大盘（可选 Flink）
     │
     └─→ 审计 / 计费 sink
```

### 3.3 调用日志查询场景

| 场景 | 查询路径 | 时延目标 |
|------|---------|---------|
| 查单次调用（trace_id） | `WHERE trace_id = '...'` | < 1s |
| 按调用方 + 时间范围 | `WHERE app_id = ? AND ts BETWEEN ...` | < 2s |
| 按 API 错误率 | `GROUP BY api_id, is_success` | < 3s |
| 实时大盘（5min 滚动） | 物化视图 `api_call_stats_hourly` | < 1s |
| 复杂聚合（多维度） | ClickHouse 直查 | < 10s |

### 3.4 应用日志（Loki）

Python 服务日志输出到 stdout，由 Promtail 采集到 Loki：

```python
import structlog

log = structlog.get_logger()
log.info("api_call", trace_id=trace_id, api_id=api_id, latency_ms=42)
```

- 结构化 JSON
- 强制带 trace_id（关联调用日志）
- 日志级别：DEBUG / INFO / WARNING / ERROR
- 生产环境默认 INFO，关键服务 DEBUG 可临时开

### 3.5 错误堆栈（MinIO）

异常时：
1. 完整堆栈序列化为 JSON
2. 上传 MinIO `call-bodies/{env}/{date}/{trace_id}.error.json`
3. ClickHouse 调用日志记录引用 `error_stack_ref`
4. 后台查看时按 trace_id 取

避免大堆栈塞爆 ClickHouse。

## 4. 指标监控

### 4.1 Prometheus 架构

```
Python 服务（/metrics）→ Prometheus 抓取 → 存储 → Grafana / AlertManager
                          ↓
                       远程存储（可选 VictoriaMetrics）
```

### 4.2 核心指标分层

**业务层**（自定义）：

| 指标 | 类型 | 说明 |
|------|------|------|
| `apihub_http_requests_total` | Counter | 调用次数（labels: api_id, app_id, status, env） |
| `apihub_http_request_duration_seconds` | Histogram | 调用延迟 |
| `apihub_request_size_bytes` | Histogram | 请求大小 |
| `apihub_retry_attempts_total` | Counter | 重试次数 |
| `apihub_retry_exhausted_total` | Counter | 重试耗尽次数 |
| `apihub_quota_exceeded_total` | Counter | 限流次数 |
| `apihub_task_duration_seconds` | Histogram | 异步任务耗时 |
| `apihub_kafka_lag` | Gauge | Kafka 消费延迟 |

**中间件层**：

| 指标 | 来源 |
|------|------|
| PostgreSQL QPS / 连接数 / 复制延迟 | postgres_exporter |
| Redis 命令数 / 命中率 / 内存 | redis_exporter |
| Kafka 分区 / 副本 / lag | kafka_exporter |
| ClickHouse part 数 / merge 速度 | clickhouse_exporter |

**K8s 层**：

| 指标 | 来源 |
|------|------|
| Pod CPU / 内存 / 网络 | kubelet cAdvisor |
| 节点资源 | node_exporter |
| Pod 状态 / 重启 | kube-state-metrics |

### 4.3 Grafana 大盘

必备 Dashboard：

| Dashboard | 内容 |
|-----------|------|
| 平台总览 | 总 QPS、错误率、P99、活跃调用方数 |
| 单 API 详情 | 单接口的 QPS / 延迟 / 错误率 / 调用方 TOP10 |
| 单应用详情 | 单调用方的用量 / 配额使用 / 错误率 |
| 调用日志 | 实时调用流（最近 1h 错误） |
| 重试监控 | 失败任务、重试成功率、死信队列 |
| Kafka 监控 | 分区 lag、消费速率、broker 健康 |
| 数据库监控 | PG / Redis / ClickHouse 健康 |
| K8s 集群 | 节点资源、Pod 异常 |

## 5. 链路追踪

### 5.1 OpenTelemetry

所有 Python 服务接入 OTel SDK：

```python
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor

FastAPIInstrumentor.instrument_app(app)
HTTPXClientInstrumentor().instrument()
AsyncPGInstrumentor().instrument()
RedisInstrumentor().instrument()
```

自动注入 trace context 到：
- HTTP Header（`traceparent`）
- Kafka message header
- 日志（trace_id 字段）

### 5.2 trace_id 贯穿

```
APISIX 生成 X-Trace-Id
    │
    ├─ HTTP Header 透传到业务服务
    │
    ├─ Kafka message header（trace_id）
    │
    ├─ PG 日志字段（trace_id）
    │
    ├─ ClickHouse 行（trace_id）
    │
    ├─ Jaeger span tags（trace_id）
    │
    └─ 错误堆栈 MinIO 文件名（trace_id）
```

任一节点都可凭 trace_id 串联完整链路。

### 5.3 Span 采样

- 全量采集：错误 / 慢调用（> 1s）
- 抽样采集：正常调用 1%（节省 Jaeger 存储）
- 通过 OpenTelemetry Collector 配置

### 5.4 查询入口

后台 / 门户 → 输入 trace_id → 跳转 Jaeger UI（带 trace_id 过滤）。

## 6. 告警体系

### 6.1 告警分级

| 级别 | 触发条件 | 通知方式 | 响应 |
|------|---------|---------|------|
| P0 | 平台不可用 / 数据丢失 | 电话 + 短信 + 钉钉 | 5min |
| P1 | 核心功能严重异常 | 短信 + 钉钉 | 15min |
| P2 | 局部异常 / 性能下降 | 钉钉 | 1h |
| P3 | 容量预警 | 钉钉 | 1 工作日 |

### 6.2 告警规则示例

```yaml
groups:
- name: api-critical
  rules:
  - alert: ApiHighErrorRate
    expr: |
      sum(rate(apihub_http_requests_total{status=~"5.."}[5m])) by (api_id)
      / sum(rate(apihub_http_requests_total[5m])) by (api_id) > 0.05
    for: 2m
    labels: { severity: P1 }
    annotations:
      summary: "API {{ $labels.api_id }} 5xx 错误率 > 5%"
      description: "5min 错误率 {{ $value | humanizePercentage }}"

  - alert: ApiHighLatency
    expr: |
      histogram_quantile(0.99, rate(apihub_http_request_duration_seconds_bucket[5m])) > 1
    for: 5m
    labels: { severity: P2 }

  - alert: RetryQueueBacklog
    expr: apihub_retry_pending > 1000
    for: 10m
    labels: { severity: P2 }

  - alert: KafkaLag
    expr: kafka_consumergroup_lag > 30000
    for: 5m
    labels: { severity: P1 }

  - alert: RedisMemoryHigh
    expr: redis_memory_used_bytes / redis_memory_max_bytes > 0.85
    for: 5m
    labels: { severity: P2 }
```

### 6.3 告警治理

- 同类告警合并（避免轰炸）
- 静默期（夜间 P3 不打扰）
- 自动恢复通知
- 告警按值班表路由（不同团队不同渠道）

## 7. 安全设计

### 7.1 鉴权矩阵

| 调用场景 | 推荐方式 |
|---------|---------|
| 内部服务 ↔ 内部服务 | JWT（短期，K8s ServiceAccount 签发） |
| 内部应用调用 API | APIKey |
| 外部应用调用 API | APIKey |
| 金融 / 高安全合作 | HMAC 签名 |
| 第三方代用户调用 | OAuth2 Authorization Code |
| Webhook 回调 | HMAC 签名（平台用 secret 签 body，调用方验签） |

### 7.2 API Key 设计

格式：`ak_{32位随机字符}`，长度 35。

存储：
- 数据库存 SHA256 hash
- 明文只在创建时显示一次
- 显示时只展示前 8 位（`ak_xxxxxxxx...`）

传输：
- Header `Authorization: Bearer ak_xxx`
- 或 Header `X-API-Key: ak_xxx`
- 严禁 URL Query 传 Key（会进日志）

轮换：
- 支持双 Key 共存期
- 推荐 90 天轮换
- 怀疑泄露立即吊销

### 7.3 HMAC 签名

```python
import hmac, hashlib, time

def sign(secret, method, path, body, timestamp):
    msg = f"{method}\n{path}\n{timestamp}\n{hashlib.sha256(body).hexdigest()}"
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()

# 请求头
# X-App-Key: ak_xxx
# X-Timestamp: 1751500000       （±5min）
# X-Signature: <sign>
# X-Nonce: <random>             （防重放）
```

服务端校验：
1. 时间戳 ±5min（防重放）
2. nonce 缓存 10min（防重放）
3. 重新签名比对（防篡改）

### 7.4 限流与防 DDoS

| 层 | 限流维度 | 实现 |
|----|---------|------|
| WAF | IP / 地区 / UA | 云 WAF |
| APISIX | 调用方 × API × 时间窗 | Redis + Lua |
| 业务服务 | 单实例连接数 | gunicorn worker |
| DB | 单租户查询频率 | PG 连接池 |

DDoS 应对：
- 流量突增 → WAF 自动识别 + CDN 启用 CAPTCHA
- 恶意 IP → 加入黑名单
- API Key 异常调用 → 自动告警 + 临时限流

### 7.5 数据加密

| 数据 | 加密 |
|------|------|
| 传输 | TLS 1.3（强制 HTTPS） |
| API Key 数据库存储 | SHA256 hash |
| Webhook secret | KMS 加密 |
| 数据库备份 | RDS 自带加密 |
| OSS 对象 | 服务端加密（SSE-KMS） |
| 跨可用区复制 | TLS |

### 7.6 敏感数据脱敏

请求 / 响应 body 入日志前，按接口配置脱敏规则：

```yaml
# 接口元数据中声明
masking:
  request:
    - field: password
      action: remove           # 完全移除
    - field: phone
      action: mask             # 138****1234
    - field: id_card
      action: hash             # SHA256
  response:
    - field: token
      action: remove
```

### 7.7 PII 数据保护

- 调用方手机号、身份证、银行卡等 PII：
  - 入库前 hash
  - 展示时脱敏
  - 不进 ClickHouse 调用日志（按字段配置）
- 后台访问 PII 需 RBAC 权限 + 审计

### 7.8 SQL 注入 / XSS / CSRF

- SQL：强制用 ORM 或参数化查询（asyncpg 的 `$1` 占位）
- XSS：Vue 模板自动转义 + CSP Header
- CSRF：API 走 Bearer Token，不用 Cookie，天然防 CSRF

### 7.9 漏洞管理

- 镜像漏洞扫描：Harbor + Trivy
- 依赖漏洞：safety / pip-audit（CI 集成）
- 容器逃逸：K8s SecurityContext（非 root + readOnlyRootFilesystem）
- 网络隔离：NetworkPolicy（默认拒绝）

## 8. 审计（[ADR-010 等保 2.0 三级](00-decisions.md#adr-010-数据合规) + [ADR-009 多租户](00-decisions.md#adr-009-多租户策略)）

### 8.1 审计范围

所有"配置变更"操作必须审计：

| 类别 | 例子 |
|------|------|
| 接口 | 创建 / 修改 / 发布 / 下线 / 回滚 |
| 应用 | 创建 / 修改 / 吊销 |
| Key | 生成 / 吊销 |
| 授权 | 授予 / 撤销 |
| 配额 | 调整 |
| 配置 | 限流规则、灰度策略 |
| 权限 | 用户角色变更 |
| **租户**（[ADR-009](00-decisions.md#adr-009-多租户策略)） | 创建 / 修改 / 暂停 / 关闭 / 成员变更 / 配额调整 |
| **跨租户访问** | 超管切换租户、跨租户查询 / 导出（额外审计 + 告警） |
| **AI 网关**（[ADR-004](00-decisions.md#adr-004-ai-网关扩展)） | LLM 调用记录、Token 使用、模型变更 |

### 8.2 审计记录字段

```sql
audit_log (
  tenant_id,                              -- ⭐ 强制带（多租户）
  actor_type, actor_id, actor_name, actor_ip,
  action, resource_type, resource_id, resource_name,
  env, detail (before/after diff),
  auth_method,                            -- 鉴权方式（等保三级要求）
  user_agent, request_id, created_at
)
```

### 8.3 多租户审计的特殊性

- **租户内查询**：用户只能看本租户审计日志（`WHERE tenant_id = ?`）
- **跨租户访问审计**：超管跨租户操作必须**额外审计** + 实时告警
- **租户管理员**可见本租户全部审计
- **平台运维**可见所有租户审计（但每次访问都留痕）

### 8.4 查询与合规

- 后台审计页：按操作者 / 时间 / 资源 / 动作 / 租户筛选
- 导出（管理员权限，按租户隔离）
- 合规：满足等保 2.0 三级要求（保留 ≥ 180 天）
- 保留期：**在线 6 个月（合规底线）+ 12 个月温数据 + OSS 永久归档**

## 9. 合规

| 标准 | 要求 |
|------|------|
| 等保 2.0 三级 | 审计 + 加密 + 访问控制 + 备份 |
| GDPR（如有海外业务） | 数据可删除、可导出 |
| PCI-DSS（如涉支付） | 详见 PCI-DSS 标准 |
| 个人信息保护法 | PII 加密、最小化收集 |

## 10. 密钥管理

### 10.1 KMS

- 阿里云 KMS 托管
- 主密钥轮换 90 天
- 应用通过 RAM Role 访问，不直接持有

### 10.2 Sealed Secrets

K8s Secret 用 Bitnami Sealed Secrets 加密存 Git：

- GitOps 友好
- 私钥在集群内解密
- 不同环境独立密钥对

## 11. 安全审计与渗透测试

- 上线前：第三方渗透测试
- 上线后：每季度内部安全审计
- Bug Bounty：可选（开 external portal 后启用）
