"""R3e 微基准:identity / pipeline / resolver × L1 on/off × 命中态,打真 Redis。

跑: .venv/bin/python -m benchmarks.bench_identity
依赖: docker apihub-redis 在 localhost:16380 (pwd apihub_dev_pwd)。
"""

import asyncio
import json
import os
import time

# 必须在 import apihub_core 前设 env
os.environ.setdefault("REDIS_HOST", "localhost")
os.environ.setdefault("REDIS_PORT", "16380")
os.environ.setdefault("REDIS_PASSWORD", "apihub_dev_pwd")
os.environ.setdefault("HMAC_SECRET_KEY", "a" * 64)
import logging as _logging

_logging.disable(_logging.CRITICAL)
for k in ("PG_HOST", "PG_USER", "PG_PASSWORD"):
    os.environ.setdefault(k, "x")

from apihub_core import crypto, identity  # noqa: E402
from apihub_core import redis as redis_mod  # noqa: E402
from apihub_core.config import get_settings  # noqa: E402
from apihub_core.l1 import TTLCache  # noqa: E402

from benchmarks._stats import (  # noqa: E402  (env 须先于 import apihub_core)  # noqa: E402
    bench,
    row,
    summary,
)

APIKEY = "ak_bench_zzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"
ID = {
    "is_active": True,
    "tenant_id": "t_bench",
    "tenant_type": "internal",
    "app_id": "app_bench",
    "is_platform_admin": False,
    "scopes": [],
    "expires_at": None,
    "key_id": "key_bench",
    "hmac_enrolled": False,
}


async def main() -> None:
    from apihub_core.logging import configure_logging

    configure_logging(level="WARNING")  # 静默 R3e debug 埋点,免污染计时
    s = get_settings()
    await redis_mod.init_redis(s)
    # seed Redis(身份 + secret)
    await identity.write_identity(APIKEY, ID, ttl=600)
    await identity.write_hmac_secret(APIKEY, crypto.encrypt_secret("bench_secret"), ttl=600)

    results = []
    print("=== A. read_identity ===")
    # L1 on, hit
    identity.configure_l1(identity=TTLCache(ttl=60), secret=TTLCache(ttl=60))
    identity._identity_l1.set(APIKEY, ID)
    results.append(
        row(
            "A1 read_identity  L1=on hit(dict)", await bench(lambda: identity.read_identity(APIKEY))
        )
    )
    # L1 on, miss → Redis
    # L1 off (= 每请求打 Redis,真实 miss 成本)
    identity.configure_l1(identity=None, secret=None)
    a_off = row(
        "A3 read_identity  L1=off(→Redis)", await bench(lambda: identity.read_identity(APIKEY))
    )

    print("\n=== B. read_identity_and_hmac_secret (pipeline) ===")
    identity.configure_l1(identity=TTLCache(ttl=60), secret=TTLCache(ttl=60))
    identity._identity_l1.set(APIKEY, ID)
    identity._secret_l1.set(APIKEY, "encblob")
    results.append(
        row(
            "B1 pipeline      L1=on both-hit(0 RTT)",
            await bench(lambda: identity.read_identity_and_hmac_secret(APIKEY)),
        )
    )
    identity.configure_l1(identity=None, secret=None)
    results.append(
        row(
            "B3 pipeline      L1=off(1 pipeline)",
            await bench(lambda: identity.read_identity_and_hmac_secret(APIKEY)),
        )
    )

    # C. resolver snapshot (best-effort — 需 dispatcher 包可 import)
    print("\n=== C. resolver.resolve_by_header (snapshot) ===")
    try:
        await _bench_resolver(results)
        print("  (resolver done)")
    except Exception as e:  # noqa: BLE001
        print(f"  (resolver skipped: {type(e).__name__}: {e})")

    print("\n=== Δ 汇总(R3e 收益,p50) ===")
    summary("read_identity  L1 on hit vs off", a_off, results[0])

    print("\n=== D. 并发(20 worker) read_identity ===")
    import asyncio as _aio

    async def _run_conc(l1_on, n_workers=20, n=400):
        if l1_on:
            identity.configure_l1(identity=TTLCache(ttl=60), secret=TTLCache(ttl=60))
            identity._identity_l1.set(APIKEY, ID)
        else:
            identity.configure_l1(identity=None, secret=None)

        async def _w():
            xs = []
            for _ in range(n):
                t = time.perf_counter_ns()
                await identity.read_identity(APIKEY)
                xs.append(time.perf_counter_ns() - t)
            return xs

        allx = sorted(x for xs in await _aio.gather(*[_w() for _ in range(n_workers)]) for x in xs)

        def pct(p):
            return allx[min(len(allx) - 1, int(len(allx) * p))] / 1e3

        return pct(0.5), pct(0.99)

    ch = await _run_conc(True)
    co = await _run_conc(False)
    print(f"  D1 conc(20w) read_identity L1=on hit : p50={ch[0]:8.2f}  p99={ch[1]:8.2f} µs")
    print(f"  D2 conc(20w) read_identity L1=off    : p50={co[0]:8.2f}  p99={co[1]:8.2f} µs")
    print(
        f"  → 并发下 p99 放大: off {co[1]:.0f}µs vs on {ch[1]:.0f}µs ({co[1]/ch[1]:.0f}x — Redis 连接竞争被 L1 消除)"
    )

    await redis_mod.close_redis()


async def _bench_resolver(results: list) -> None:
    import dataclasses

    from dispatcher import resolver
    from dispatcher.models import ApiVersionSnapshot

    snap = ApiVersionSnapshot(
        id="v_bench",
        api_id="api_bench",
        tenant_id="t_bench",
        version="1",
        backend_type="http",
        backend_url="http://mock/up",
        method="POST",
        path="/up",
        masking=None,
        rate_limit=None,
        retry_policy=None,
        cache_policy=None,
        ai_model=None,
        ai_streaming=False,
        ai_params=None,
        sla_p99_ms=None,
        sla_availability=None,
        timeout_ms=30000,
        visibility="public",
    )
    vid = "v_bench"
    await redis_mod.t_set(f"snapshot:{vid}", json.dumps(dataclasses.asdict(snap)), ex=600)

    resolver.configure_snapshot_l1(TTLCache(ttl=60))
    resolver._snapshot_l1.set(f"snapshot:{vid}", dataclasses.asdict(snap))
    results.append(
        row(
            "C1 resolve_by_hdr L1=on hit(dict)",
            await bench(lambda: resolver.resolve_by_header(vid)),
        )
    )
    resolver._snapshot_l1.clear()
    results.append(
        row(
            "C2 resolve_by_hdr L1=on miss(→Redis)",
            await bench(lambda: resolver.resolve_by_header(vid)),
        )
    )
    resolver.configure_snapshot_l1(None)
    results.append(
        row(
            "C3 resolve_by_hdr L1=off(→Redis)", await bench(lambda: resolver.resolve_by_header(vid))
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
