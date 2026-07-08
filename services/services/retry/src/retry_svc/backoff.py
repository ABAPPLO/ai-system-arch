"""退避算法 —— 给下次重试计算延迟。

支持 exponential（默认，jittered）/ fixed / linear。
"""

import random

from retry_svc.models import BackoffPolicy


def compute_delay_ms(
    attempt_no: int,
    *,
    policy: BackoffPolicy | str = BackoffPolicy.EXPONENTIAL,
    base_ms: int = 1000,
    cap_ms: int = 60_000,
) -> int:
    """计算第 attempt_no 次重试的延迟毫秒数。

    attempt_no 是即将执行的那一次（1-based）。
    cap_ms 给个上限避免指数爆炸（生产 60s 已够）。
    """
    policy = BackoffPolicy(policy) if isinstance(policy, str) else policy

    if attempt_no < 1:
        attempt_no = 1

    if policy == BackoffPolicy.FIXED:
        delay = base_ms
    elif policy == BackoffPolicy.LINEAR:
        delay = base_ms * attempt_no
    else:  # exponential
        # base * 2^(attempt_no-1)，加 ±25% jitter 防止雪崩同步
        raw = base_ms * (2 ** (attempt_no - 1))
        jitter = random.uniform(0.75, 1.25)  # noqa: S311
        delay = int(raw * jitter)

    return min(delay, cap_ms)


def next_attempt_delay_ms(
    retry_count: int,
    *,
    policy: BackoffPolicy | str = BackoffPolicy.EXPONENTIAL,
    base_ms: int = 1000,
    cap_ms: int = 60_000,
) -> int:
    """下一个 attempt 的延迟。retry_count 是已经失败过的次数。

    即将执行的是第 retry_count+1 次。
    """
    return compute_delay_ms(
        retry_count + 1, policy=policy, base_ms=base_ms, cap_ms=cap_ms
    )
