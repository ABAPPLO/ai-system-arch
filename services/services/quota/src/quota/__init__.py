"""quota —— 配额与限流服务。

职责：
1. 3-tier 限流（秒 / 分 / 日）+ 多维度（tenant / app / api）
2. Redis Lua 原子 check+incr，P99 < 1ms
3. 配额规则合并：app > tenant > api_version > default
4. 调用计数推 Kafka api-call-events（给 ClickHouse 计费）
5. 用量查询（admin / 用户）

详见 docs/03-services.md §3.6 + docs/06-high-concurrency.md §7 + docs/04-data-model.md §5.4。
"""
