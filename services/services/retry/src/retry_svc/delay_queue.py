"""Redis ZSet 延迟队列 —— 跨租户操作（worker 消费端，无 TenantContext）。

key 规范（docs/04 §5.5）：
  t:{tenant_id}:retry:delayed     → ZSet, member = retry_task_id, score = next_attempt_at unix_ts
  t:{tenant_id}:retry:processing  → Set,  正在处理的 retry_task_id

跨租户：直接用 raw_client() 不走前缀封装（worker 拿到 tenant_id 后自己拼 key）。
"""

import time

from apihub_core import redis as redis_mod
from apihub_core.logging import get_logger

log = get_logger(__name__)

DELAYED_SUFFIX = ":retry:delayed"        # score = due_ts
PROCESSING_SUFFIX = ":retry:processing"  # in-flight set


def _key(tenant_id: str, suffix: str) -> str:
    return f"t:{tenant_id}{suffix}"


async def schedule(
    *,
    tenant_id: str,
    retry_task_id: int,
    next_attempt_at_ts: float,
) -> None:
    """推入延迟队列。next_attempt_at_ts 是 unix 时间戳（秒）。"""
    client = redis_mod.raw_client()
    key = _key(tenant_id, DELAYED_SUFFIX)
    await client.zadd(key, {str(retry_task_id): next_attempt_at_ts})
    log.debug(
        "delay_queue_pushed",
        tenant_id=tenant_id,
        retry_task_id=retry_task_id,
        due_at=next_attempt_at_ts,
    )


async def pop_due(
    *,
    tenant_id: str,
    max_count: int = 10,
    now_ts: float | None = None,
) -> list[int]:
    """取出到期的任务（score <= now），加入 processing set，返回 retry_task_id 列表。

    使用 ZPOPMIN + ZADD（processing）+ pipeline，避免竞态。
    简化实现：ZRANGEBYSCORE 取 + ZREM 原 set + SADD processing。
    """
    client = redis_mod.raw_client()
    delayed_key = _key(tenant_id, DELAYED_SUFFIX)
    processing_key = _key(tenant_id, PROCESSING_SUFFIX)

    now = now_ts if now_ts is not None else time.time()

    # 原子取到期任务：LUA 脚本保证 ZRANGEBYSCORE + ZREM + SADD 一起跑
    lua = """
        local due = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1], 'LIMIT', 0, ARGV[2])
        if #due == 0 then
            return {}
        end
        redis.call('ZREM', KEYS[1], unpack(due))
        for i = 1, #due do
            redis.call('SADD', KEYS[2], due[i])
        end
        return due
    """
    raw = await client.eval(lua, 2, delayed_key, processing_key, now, max_count)
    return [int(x) for x in raw]


async def complete(
    *,
    tenant_id: str,
    retry_task_id: int,
) -> None:
    """处理完（成功或失败都算）→ 从 processing set 移除。"""
    client = redis_mod.raw_client()
    processing_key = _key(tenant_id, PROCESSING_SUFFIX)
    await client.srem(processing_key, str(retry_task_id))


async def is_processing(*, tenant_id: str, retry_task_id: int) -> bool:
    client = redis_mod.raw_client()
    return await client.sismember(
        _key(tenant_id, PROCESSING_SUFFIX), str(retry_task_id)
    )


async def list_tenants_with_pending() -> list[str]:
    """SCAN 所有 t:*:retry:delayed key，提取 tenant_id 列表。

    worker 用这个分片扫描多个租户的延迟队列。

    key 格式：t:{tenant_id}:retry:delayed
        tenant_id 可能是 'tenant_a' 这种文本 ID，不能假设是整数。
    """
    client = redis_mod.raw_client()
    seen: set[str] = set()
    async for key in client.scan_iter(match="t:*:retry:delayed", count=200):
        try:
            parts = key.split(":")
            # parts[0]='t' parts[1]=tenant_id parts[2]='retry' parts[3]='delayed'
            if len(parts) >= 4:
                seen.add(parts[1])
        except IndexError:
            log.warning("delay_queue_unparsable_key", key=key)
            continue
    return sorted(seen)


async def get_due_count(*, tenant_id: str, now_ts: float | None = None) -> int:
    """查 ZSet 中已到期的元素数（监控用，不取数据）。"""
    client = redis_mod.raw_client()
    now = now_ts if now_ts is not None else time.time()
    return await client.zcount(_key(tenant_id, DELAYED_SUFFIX), "-inf", now)
