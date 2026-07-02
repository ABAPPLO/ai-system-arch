"""Lua 脚本 —— 多 tier 原子 check+incr / refund。

Redis 单线程执行 Lua，无竞态；一次 RTT 完成 N 个 tier 的检查 +
INCR + EXPIRE，把跨网络往返的延迟省掉。

详见 docs/06-high-concurrency.md §7.3。
"""

# 多 tier 原子 check + incr
#
# 入参：
#   KEYS[1..n]    N 个限流 key（按 tier 顺序：second/minute/day）
#   ARGV[1..n]    每个 tier 的 max_count（0 = 不限）
#   ARGV[n+1..2n] 每个 tier 的 TTL（秒）
#   ARGV[2n+1]    本次 INCR 的 cost
#
# 返回（数组）：
#   {0, ...}                  通过：所有 tier 都没超
#   {tier_idx, ttl, current}  第 tier_idx 层（1-based）超了，current = 当前已用
#
# 设计要点：
#   - 第一次 INCR 设 TTL（滑动 reset）；后续 INCR 不重置 TTL，保证窗口正确闭合
#   - 超了立即返回，**已 INCR 的其他 tier 不 refund**（少扣一点点，但限流场景
#     这是安全的；客户端 retry 时反正还会重新扣）
#   - max=0 表示该 tier 不限流，跳过检查但仍 INCR 计数（给 usage 查询用）
CHECK_AND_INCR = """
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
"""


# 退回（refund）—— 调用失败时退回 cost。INCRBY 负数，不低于 0。
# 不删 key（保留窗口期），只把数字回退。
REFUND = """
local n = #KEYS
local cost = tonumber(ARGV[1])

for i = 1, n do
    local current = tonumber(redis.call('GET', KEYS[i]) or '0')
    local new_val = current - cost
    if new_val < 0 then new_val = 0 end
    redis.call('SET', KEYS[i], new_val)
end

return 1
"""


# 读多 tier 当前用量（不 INCR，只 GET + TTL）。
# 返回 {tier1_used, tier1_ttl, tier2_used, tier2_ttl, ...}
READ_USAGE = """
local result = {}
for i = 1, #KEYS do
    local used = redis.call('GET', KEYS[i]) or '0'
    local ttl = redis.call('TTL', KEYS[i])
    table.insert(result, used)
    table.insert(result, ttl)
end
return result
"""
