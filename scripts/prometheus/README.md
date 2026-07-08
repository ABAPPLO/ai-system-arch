# Prometheus + Alertmanager 配置

> APIHub 告警体系配置（详见 [docs/08-observability-security.md §6](../../docs/08-observability-security.md)）。

## 目录结构

```
scripts/prometheus/
├── prometheus.yml           # 主配置：scrape + rule_files + alerting
├── alertmanager.yml         # 路由：P0→电话、P1→短信、P2→钉钉（按 team）
├── rules/
│   ├── api-critical.yml     # P0 / P1：平台宕机、5xx 飙升、auth fail
│   ├── api-quality.yml      # P2：P99 延迟、限流比例、AI token 消耗
│   ├── infra.yml            # P1 / P2：Kafka lag、PG 连接、Redis、Node 资源
│   └── business.yml         # P1 / P2：retry 死信、workflow 失败、masking 失败
└── templates/
    └── dingtalk.tmpl        # 钉钉 markdown 模板（severity/api_id/runbook）
```

## 校验

```bash
# CI 用：YAML 语法 + 字段必填 + severity 白名单 + 重复名检测
python scripts/validate-alerts.py
```

输出示例：
```
✅ api-critical.yml: 6 rules (P0=3, P1=3)
✅ api-quality.yml: 5 rules (P2=5)
✅ business.yml: 6 rules (P1=2, P2=4)
✅ infra.yml: 8 rules (P1=4, P2=4)

✨ 25 rules across 4 files
```

## 告警分级（ADR-locked）

| 级别 | 触发条件 | 通知 | 响应 SLA |
|------|---------|------|---------|
| **P0** | 平台不可用 / 数据丢失 | 电话 + 短信 + 钉钉 | 5min |
| **P1** | 核心功能严重异常 | 短信 + 钉钉 | 15min |
| **P2** | 局部异常 / 性能下降 | 钉钉 | 1h |
| P3 | 容量预警 | 钉钉（夜间静默） | 1 工作日 |

## 现有规则索引

### P0（3 条 · `api-critical.yml`）
- `ApiHubPlatformDown` — 5min 内无 scrape
- `ApiHubErrorRateSaturated` — 全平台 5xx > 50%
- `DispatcherDown` — dispatcher Pod 不可达

### P1（9 条 · `api-critical` + `infra` + `business`）
- `ApiHighErrorRate` — 单 API 5xx > 5%
- `ApiAuthFailureSpike` — 鉴权失败激增
- `ApiQuotaExhaustedSpree` — 多 app 配额同时耗尽
- `KafkaConsumerLag` — 消费滞后 > 30k
- `KafkaNoConsumer` — 消费组空
- `PgConnectionPoolExhausted` — 连接池 > 90%
- `ClickHouseUnavailable` — CH 5xx
- `RetryDeadLetterSpike` — 死信激增
- `WorkflowFailureSpike` — Workflow 失败率 > 30%

### P2（13 条 · `api-quality` + `infra` + `business`）
- 延迟：P99 > 1s / P95 > 500ms
- 容量：限流比例、429、AI token burn rate
- 异步：task 失败率、retry queue backlog
- 基础设施：Redis 内存、连接数、Node 磁盘 / CPU
- 业务：change_request backlog、dispatcher forward 失败、AI masking 失败

## 添加新告警的流程

1. 选合适的文件（按子系统 / 严重程度）：
   - 新 API 类指标 → `api-critical`（P0/P1）或 `api-quality`（P2）
   - 新基础设施指标 → `infra`
   - 新业务子系统（retry/workflow/dispatcher 等）→ `business`
2. 加规则：
   ```yaml
   - alert: MyNewAlert
     expr: my_metric > 1
     for: 5m
     labels:
       severity: P2  # P0/P1/P2/P3
       team: platform  # platform / trading / risk
     annotations:
       summary: "一句话摘要（钉钉标题）"
       description: "详细描述 + 排查指引（钉钉正文）"
   ```
3. 跑校验：`python scripts/validate-alerts.py`
4. 测试触发：在 dev/staging 环境制造条件，看是否真的 firing
5. 提 PR：标题 `[alerts] add XxxAlert P{级别}`

## 团队路由

按 `labels.team` 路由到不同钉钉群：

| team | 群 | 适用场景 |
|------|----|----|
| `platform` | 平台 SRE 群 | 基础设施 / API 整体 / 所有 P0 |
| `trading` | 交易线群 | 交易相关业务告警 |
| `risk` | 风控群 | 风控相关告警 |

新团队需要在 `alertmanager.yml` 加 receiver + route。

## 抑制规则

- 同 `api_id` + `team` 的告警，**高级别抑制低级别**：
  - P0 firing 时，同 API 的 P1/P2/P3 不再发
  - P1 firing 时，同 API 的 P2/P3 不再发
- 避免一条上游故障 → 全平台告警风暴

## 静默期（silence）

紧急发版 / 维护窗口期间，运维通过 Alertmanager API / UI 加 silence：

```bash
amtool silence add --comment "deploy v1.2.3" \
  --duration=30m \
  alertname=~"Api.*" \
  --alertmanager.url=http://alertmanager:9093
```

## secret 注入

`alertmanager.yml` 中的 `${DINGTALK_*_URL}` 通过环境变量注入（K8s Secret → env）：

```yaml
# K8s Secret（占位）
apiVersion: v1
kind: Secret
metadata:
  name: alertmanager-dingtalk
  namespace: monitoring
stringData:
  DINGTALK_P0_URL: https://oapi.dingtalk.com/robot/send?access_token=xxx
  DINGTALK_P1_URL: ...
```

Alertmanager Deployment 用 `--config.expand-env` 或 `envsubst` 启动时替换。
