"""微基准统计 helper —— 纯 stdlib，无新依赖。

warmup 后跑 n 轮，收每调用 ns，算 min/p50/p99/mean(µs)。
fn 可以是 sync 或「返 coroutine 的 callable」(如 lambda 包 async 函数)。
"""

import asyncio
import statistics
import time


async def bench(fn, *, n_warmup: int = 200, n: int = 2000) -> dict:
    async def call() -> None:
        r = fn()
        if asyncio.iscoroutine(r):
            await r

    for _ in range(n_warmup):
        await call()
    xs: list[int] = []
    for _ in range(n):
        t = time.perf_counter_ns()
        await call()
        xs.append(time.perf_counter_ns() - t)
    xs.sort()

    def pct(p: float) -> int:
        return xs[min(len(xs) - 1, int(len(xs) * p))]

    return {
        "n": n,
        "min_us": xs[0] / 1e3,
        "p50_us": pct(0.50) / 1e3,
        "p99_us": pct(0.99) / 1e3,
        "mean_us": statistics.mean(xs) / 1e3,
    }


def row(label: str, r: dict) -> dict:
    print(
        f"  {label:<42} min={r['min_us']:8.3f}  p50={r['p50_us']:8.3f}  "
        f"p99={r['p99_us']:8.3f}  mean={r['mean_us']:8.3f} µs"
    )
    return {"label": label, **r}


def summary(delta_label: str, base: dict, cmp: dict) -> None:
    d = base["p50_us"] - cmp["p50_us"]
    pct = d / base["p50_us"] * 100 if base["p50_us"] else 0
    print(f"  → {delta_label}: p50 Δ = {d:+.2f} µs (L1 hit 省 {pct:.1f}% vs off)")
