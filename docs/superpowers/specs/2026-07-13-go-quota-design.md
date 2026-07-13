# Phase 4「Go 重写 quota」设计

> 日期：2026-07-13
> 阶段：Phase 4 演进 — Go 重写 quota（热点服务性能优化）
> 关联：docs/06-high-concurrency.md、docs/03-services.md

## 1. Goal

将现有 Python FastAPI quota 服务（~1557 行）重写为 Go，降低 P99 延迟、减少资源占用。保持 API 兼容，现有调用方（dispatcher）不改代码。

### 1.1 现有服务

| 指标 | Python 版 |
|------|-----------|
| 代码量 | ~1557 行（含测试 680） |
| 框架 | FastAPI + uvicorn |
| 核心路径 | PG 查限流规则 → Redis Lua EVALSHA → Kafka emit |
| P99 延迟 | ~15-50ms |
| 内存 | ~80-120MB |
| 启动时间 | ~3-8s |

### 1.2 本切片做

- **Go quota-svc** — 替代 Python quota 服务，部署在相同端口（8004）
- **精简 API** — 只保留核心限流路径：check / check-strict / refund / usage / health
- **API 兼容** — 请求/响应格式与 Python 版一致，dispatcher 零改动
- **Redis Lua 复用** — 三个脚本（CHECK_AND_INCR / REFUND / READ_USAGE）作为 Go 常量内嵌

### 1.3 移除（由 billing-svc 承担）

| Python 端点 | 替代 |
|-------------|------|
| `GET /v1/quota/billing` | billing-svc `/v1/billing/records` |
| `GET /v1/quota/plans` | portal-bff `/v1/portal/plans` |
| 定时出账 cron | billing-svc `POST /v1/billing/periodic` |

### 1.4 成功标准

- ✅ API 兼容：dispatcher 调 `POST /v1/quota/check` 得到与 Python 版相同的响应
- ✅ P99 延迟 ≤ 5ms
- ✅ Go 服务内存 ≤ 30MB
- ✅ 现有 portal/admin 不破
- ✅ go vet + staticcheck clean

## 2. 架构

```
┌─ dispatcher ─────────────────────────────────────────┐
│  POST /v1/quota/check → Go quota (port 8004)         │
│  POST /v1/quota/refund → Go quota                    │
│  GET  /v1/quota/usage  → Go quota                    │
└────────────────────────┬────────────────────────────┘
                         │
                         ▼
┌──────────────────────────────────────────────────────┐
│            Go quota-svc (net/http)                    │
│                                                      │
│  POST /v1/quota/check      → PG 查规则 → Redis Lua  │
│  POST /v1/quota/check-strict→ PG 查规则 → Redis Lua  │
│  POST /v1/quota/refund     → Redis Lua               │
│  GET  /v1/quota/usage      → Redis Lua               │
│  GET  /v1/quota/health     → {"status":"ok"}         │
└──────────┬──────────────────────────┬────────────────┘
           │ pgx                      │ go-redis
           ▼                          ▼
    ┌──────────────┐         ┌──────────────┐
    │  PostgreSQL   │         │    Redis      │
    │  (限流规则)    │         │  (计数器)     │
    └──────────────┘         └──────────────┘
```

### 2.1 请求流（check）

```
dispatcher POST /v1/quota/check
  → Go handler 解析 JSON {tenant_id, app_id, api_id, cost}
  → repository.LoadRules(tenant, app, api) — PG 查询限流规则
  → limiter.CheckAndConsume(keys, rules, cost)
      → go-redis EvalSha(CHECK_AND_INCR, keys, args)
      → Redis 执行 Lua 原子 check + INCRBY + EXPIRE
      → 返回 {allowed, tier_blocked, current, ttl}
  → [可选] kafka-go 异步 emit api-call-events
  → 返回 JSON
```

### 2.2 Lua 脚本复用

三个 Lua 脚本与 Python 版**完全一致**，作为 Go 字符串常量内嵌：

```go
var checkAndIncrScript = redis.NewScript(`
local n = #KEYS
local cost = tonumber(ARGV[2 * n + 1]) or 1
for i = 1, n do
    local max_count = tonumber(ARGV[i])
    local ttl = tonumber(ARGV[n + i])
    local current = redis.call('INCRBY', KEYS[i], cost)
    if current == cost then
        redis.call('EXPIRE', KEYS[i], ttl)
    end
    if max_count > 0 and current > max_count then
        local remaining_ttl = redis.call('TTL', KEYS[i])
        if remaining_ttl < 0 then remaining_ttl = ttl end
        return {i, remaining_ttl, current}
    end
end
return {0, 0, 0}
`)
```

## 3. API 端点

### 3.1 `POST /v1/quota/check`

```json
// Request
{"tenant_id":"t_001","app_id":"app_trading","api_id":"api_demo","cost":1}
// Response 200
{"allowed":true,"tier_blocked":"","current":5,"limit":100,"remaining":95,"reset_ms":45000,"rule_source":"api"}
```

### 3.2 `POST /v1/quota/check-strict`

同上，超限返回 HTTP 429。

### 3.3 `POST /v1/quota/refund`

```json
{"refunded": true}
```

### 3.4 `GET /v1/quota/usage`

```json
{"points":[
  {"tier":"second","used":5,"limit":100,"remaining":95,"reset_ms":45000},
  {"tier":"minute","used":50,"limit":5000,"remaining":4950,"reset_ms":25000},
  {"tier":"day","used":500,"limit":50000,"remaining":49500,"reset_ms":3600000}
]}
```

### 3.5 `GET /v1/quota/health`

`{"status":"ok","service":"quota"}`

## 4. 文件结构

```
services/go/quota/
├── go.mod
├── cmd/
│   └── main.go                  # 入口
├── internal/
│   ├── handler/
│   │   └── quota.go             # HTTP handler（5 端点）
│   ├── limiter/
│   │   └── redis.go             # Redis Lua + rate key
│   ├── repository/
│   │   └── pg.go                # PG 规则查询
│   ├── models/
│   │   └── types.go             # 请求/响应结构体
│   └── config/
│       └── config.go            # 环境变量配置
├── Dockerfile
└── tests/
    └── quota_test.go
```

## 5. 配置项

| 变量 | 默认 | 说明 |
|------|------|------|
| `PORT` | `8004` | |
| `PG_HOST` | — | |
| `PG_PORT` | `5432` | |
| `PG_USER` | — | |
| `PG_PASSWORD` | — | |
| `PG_DATABASE` | `apihub` | |
| `PG_POOL_SIZE` | `10` | |
| `REDIS_ADDR` | — | host:port |
| `REDIS_PASSWORD` | — | |
| `KAFKA_BROKERS` | — | |
| `LOG_LEVEL` | `info` | |

## 6. 实现顺序

| # | 任务 | 文件 |
|---|------|------|
| 1 | go.mod + config.go | 2 |
| 2 | models/types.go | 1 |
| 3 | repository/pg.go | 1 |
| 4 | limiter/redis.go | 1 |
| 5 | handler/quota.go | 1 |
| 6 | cmd/main.go | 1 |
| 7 | Dockerfile | 1 |
| 8 | tests/quota_test.go | 1 |
| 9 | go vet + staticcheck | — |

## 7. 风险

| 风险 | 对策 |
|------|------|
| Go 版响应格式与 Python 版差异 | 对照 Python 单测逐字段验证 |
| Kafka 异步发送丢失 | 同 Python 版 best-effort，不阻塞 |
| Redis Lua 未加载 | startup 时 SCRIPT LOAD，失败则启动失败 |
| PG 连接池不足 | 预热连接 + 可配置 pool size |
