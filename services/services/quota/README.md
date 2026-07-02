# quota

> 配额与限流服务 —— Redis 原子 Lua 脚本做 3-tier 决策，P99 < 1ms。
> 详见 [docs/03-services.md §3.6](../../../docs/03-services.md) + [docs/04-data-model.md §5.4](../../../docs/04-data-model.md)。

## 架构

```
client → POST /v1/quota/check
              ↓
         load_rules (PG: app > tenant > api_version)
              ↓
         check_and_consume (Redis Lua: CHECK_AND_INCR)
              ↓
         allowed? → 返回 200 / 429
              ↓
         emit Kafka api-call-events（成功扣减才发）
```

整个决策一次 Redis RTT 完成 3 个 tier 的 check + INCR + EXPIRE，**没有锁、没有竞态**。

## 3-tier 限流

| tier | 窗口 | 用途 |
|------|------|------|
| second | 1s | 秒级峰值保护（防恶意刷） |
| minute | 60s | 短期突增抑制 |
| day | 86400s | 日配额（按租户计费的基础） |

三个 tier **同时**生效，按 `second > minute > day` 顺序检查。任何一层超 → 挡，返回 `tier_blocked` 字段告诉调用方是哪一层。

错误码区分：
- `tier_blocked == 'day'` → `TENANT_QUOTA_EXCEEDED (20004)`：今日配额用完，提示用户充值/升级
- 其他 → `RATE_LIMITED (10005)`：请求太快，提示 retry-after

## 多维度独立计数

key 格式：`t:{tenant_id}:rate:{api_id}:{app_id}:{s|m|d}:{slot}`

- 同租户不同 app 互不影响（app_a 流量不挤占 app_b）
- 同 app 不同 api 互不影响（高频 API 不挤占低频）
- `tenant_id` 在 key 前缀 → Redis Cluster 同租户的 key 落同 slot（cluster hash tag 可选）

时间槽 `slot = int(time / window)` —— 固定窗口策略。docs/04 推荐：实现简单，重置时机明确，配额场景下足够（不需要滑动窗口的精度）。

## 规则合并优先级

```
app.rate_limit > tenant.rate_limit > api_version.rate_limit > default
```

每层都是 JSONB：
```json
{"second": {"max_count": 10}, "minute": 100, "day": 10000}
```

支持简写（`100` 等同 `{"max_count": 100}`，window 取 tier 默认）。每 tier 独立合并（app 提升 day 不影响 second）。

## Lua 脚本（CHECK_AND_INCR）

```lua
local n = #KEYS
local cost = tonumber(ARGV[2 * n + 1]) or 1
for i = 1, n do
    local max_count = tonumber(ARGV[i])
    local ttl = tonumber(ARGV[n + i])
    local current = redis.call('INCRBY', KEYS[i], cost)
    if current == cost then
        redis.call('EXPIRE', KEYS[i], ttl)   -- 首次访问设 TTL
    end
    if max_count > 0 and current > max_count then
        local remaining_ttl = redis.call('TTL', KEYS[i])
        if remaining_ttl < 0 then remaining_ttl = ttl end
        return {i, remaining_ttl, current}   -- i = 1/2/3 = 哪一层挡
    end
end
return {0, 0, 0}   -- 全通过
```

要点：
- `max_count == 0` 跳过检查但仍 INCR（disabled tier 也保持计数，方便后续 enable）
- INCR 在 CHECK 之前 → **被挡的那次也算进计数**（slight over-count，安全有意的，见下方 refund 注释）
- `EXPIRE` 只在 `current == cost` 时设（首次访问），不每次续期（固定窗口语义）

## Redis 故障降级

```python
try:
    result = await client.eval(CHECK_AND_INCR, ...)
except Exception:
    return QuotaCheckResponse(allowed=True, rule_source="fallback")
```

**降级放行**的理由：限流挂了不应该把业务搞挂。保守策略是 allow，靠客户端 retry + 业务后端自我保护兜底。`rule_source="fallback"` 让调用方知道这次决策不可靠（可记日志/告警）。

## refund 语义（重要）

```python
async def refund(tenant_id, app_id, api_id, cost=1):
    # 退回 cost（INCRBY 负数）
```

调用业务后端失败时，**应该退的 cost 包含被挡的那次**。例：
- 第二 tier max=2，已经成功 2 次（used=2）
- 第 3 次调用 INCR→3 但被挡（业务没真调）
- 业务后端真调失败 → 退 cost=2（一次成功 + 一次被挡都退）→ used=1
- 下次调用 INCR→2，通过

被挡的那次 INCR 已经生效（Lua 设计如此），所以退不够就解锁不了。这是有意的，简化 Lua（不需要事务回滚）。

## 接口

| 方法 | 路径 | 鉴权 | 说明 |
|------|------|------|------|
| POST | `/v1/quota/check`        | 服务间 | 返回 allowed=False（语义），不抛异常 |
| POST | `/v1/quota/check-strict` | 服务间 | 超了直接抛 429（dispatcher/executor 用） |
| POST | `/v1/quota/refund`       | 服务间 | best-effort 退回 cost |
| GET  | `/v1/quota/usage`        | 租户鉴权 | 查当前用量（只能查自己，admin 可查任何） |
| GET  | `/v1/quota/billing`      | 租户鉴权 | Phase 3 实现（占位） |
| GET  | `/v1/quota/health`       | 无 | k8s probe |

`/check` `/check-strict` `/refund` 不要求鉴权 —— 这些是内部服务调的，鉴权由上游（dispatcher）做。

## 本地开发

```bash
make install
make dev-up              # 起 PG/Redis/Kafka
make run-quota           # uvicorn quota.main:app --port 8004
```

手动测一下：
```bash
# 基本放行
curl -s localhost:8004/v1/quota/check \
  -H 'Content-Type: application/json' \
  -d '{"tenant_id":"t1","app_id":"app_x","api_id":"api_users"}' | jq

# 打满秒级（max=2 → 第 3 次挡）
for i in 1 2 3; do
  curl -s localhost:8004/v1/quota/check \
    -H 'Content-Type: application/json' \
    -d '{"tenant_id":"t1","app_id":"app_x","api_id":"api_users"}' | jq .tier_blocked
done
# null, null, "second"
```

## 测试

```bash
cd services/services/quota
pytest tests/ -v
# 38 tests, all pass
```

覆盖：
- `test_rules.py`（12）—— _parse_tier（int/dict/shorthand/invalid）+ _parse_rules_blob（json string/partial/garbage）+ _merge（override/per-tier/3-layer chain）
- `test_limiter.py`（14）—— 各 tier blocked + disabled 跳过 + cost>1 + refund 不为负 + usage 不 INCR + Redis 故障降级 + slot 计算 + 多维度隔离
- `test_routes.py`（12）—— /check + /check-strict + /refund + /usage（鉴权 + 跨租户拒绝） + /billing 占位 + /health

mock 策略：
- DB 层：`monkeypatch` repository.load_rules 返回固定 QuotaRules
- Redis 层：fakeredis（支持真跑 Lua 脚本，包括 INCRBY/EXPIRE/TTL）
- Kafka 层：替换 `kafka.emit` 收集到 list 验证

## 性能预算（prod）

- 5-15 副本（docs/03 §3.6），prod 起步 8
- 单副本 2 CPU / 2Gi（Redis pipeline 不吃内存，CPU 全在 Lua eval）
- P99 < 1ms（一次 RTT + Lua 执行）
- HPA 基于 CPU（70%）+ 自定义指标 `quota_redis_p99_ms`

## 关联

- 上游：dispatcher `/v1/proxy/*` 调 check-strict，挡了直接 429 给 client
- 上游：executor 异步任务执行前也调 check（同步任务才扣，异步任务由 task 表自己管配额）
- 下游：成功扣减后 emit `api-call-events` 给 analyzer 算计费/统计（Phase 2）
- 数据：`scripts/init-db/01-schema.sql` 的 app/tenant/api_version 表的 `rate_limit JSONB` 字段
