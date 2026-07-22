"""R3e 全 HTTP e2e:真鉴权(X-Ingress-Auth → Redis 身份读)+ stubbed resolve/forward/kafka,
N 并发 × M 请求测 P50/P99,L1 on vs off。

跑: PYTHONPATH=services/services/dispatcher/src:. .venv/bin/python -m benchmarks.e2e_load
依赖: docker apihub-redis @ localhost:16380。
"""

import asyncio
import os
import time

os.environ["REDIS_HOST"] = "localhost"
os.environ["REDIS_PORT"] = "16380"
os.environ["REDIS_PASSWORD"] = "apihub_dev_pwd"  # noqa: S105  dev/bench 占位
os.environ["INGRESS_SHARED_SECRET"] = "bench_ingress_secret"  # noqa: S105  bench 占位
for k in ("PG_HOST", "PG_USER", "PG_PASSWORD"):
    os.environ.setdefault(k, "x")

from apihub_core import identity, kafka  # noqa: E402
from apihub_core import redis as rm  # noqa: E402
from apihub_core.config import get_settings  # noqa: E402
from apihub_core.l1 import TTLCache  # noqa: E402
from apihub_core.logging import configure_logging  # noqa: E402
from dispatcher import routes  # noqa: E402
from dispatcher.forwarder import HttpForwarder  # noqa: E402
from dispatcher.main import app  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

APIKEY = "ak_e2e_bench_zzzzzzzzzzzzzzzzzzzzzz"
SECRET = "bench_ingress_secret"  # noqa: S105  bench 占位
ID = {
    "is_active": True,
    "tenant_id": "t_e2e",
    "tenant_type": "internal",
    "app_id": "app_e2e",
    "is_platform_admin": False,
    "scopes": [],
    "expires_at": None,
    "key_id": "k_e2e",
    "hmac_enrolled": False,
}


class _FakeResp:
    def __init__(self):
        self.status_code = 200
        self.content = b'{"ok": true}'
        self.headers = {"content-type": "application/json"}


async def _fake_request(*a, **kw):
    return _FakeResp()


async def _resolve_stub(version_id):
    from dispatcher.models import ApiVersionSnapshot

    return ApiVersionSnapshot(
        id=version_id,
        api_id="a",
        tenant_id="t_e2e",
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


async def _swallow_emit(*a, **kw):
    pass


def pct(xs, p):
    return xs[min(len(xs) - 1, int(len(xs) * p))] / 1e3


async def run_load(client, headers, n_workers=1, n=1000):
    async def worker():
        xs = []
        for _ in range(n):
            t = time.perf_counter_ns()
            r = await client.post("/dispatch/sync", headers=headers, json={})
            xs.append(time.perf_counter_ns() - t)
            assert r.status_code == 200, r.status_code
        return xs

    allx = sorted(
        x for xs in await asyncio.gather(*[worker() for _ in range(n_workers)]) for x in xs
    )
    return pct(allx, 0.5), pct(allx, 0.99), len(allx)


async def main():
    configure_logging(level="WARNING")
    s = get_settings()
    s.ingress_shared_secret = SECRET
    await rm.init_redis(s)
    await identity.write_identity(APIKEY, ID, ttl=600)
    kafka.emit_event = _swallow_emit
    routes.resolve_by_header = _resolve_stub
    fwd = HttpForwarder(__import__("httpx").AsyncClient(trust_env=False))
    fwd._client.request = _fake_request
    routes._forwarder = fwd

    headers = {"X-API-Key": APIKEY, "X-API-Version-Id": "v_e2e", "X-Ingress-Auth": SECRET}
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://t")
    await client.post("/dispatch/sync", headers=headers, json={})  # warmup + sanity

    print(
        "=== 全 HTTP e2e /dispatch(真鉴权 X-Ingress-Auth→Redis 身份; resolve/forward/kafka stub; 顺序 1w) ==="
    )
    print("    (20 并发 ASGI in-process 会饱和事件循环~22ms,非代表性;用顺序量真 per-request 成本)")
    # L1 on
    identity.configure_l1(identity=TTLCache(ttl=60), secret=TTLCache(ttl=60))
    identity._identity_l1.set(APIKEY, ID)
    p50, p99, cnt = await run_load(client, headers)
    print(f"  L1=on  (1w×1000) : p50={p50:8.2f}  p99={p99:8.2f} µs  (n={cnt})")
    # L1 off
    identity.configure_l1(identity=None, secret=None)
    p50o, p99o, cnto = await run_load(client, headers)
    print(f"  L1=off (1w×1000) : p50={p50o:8.2f}  p99={p99o:8.2f} µs  (n={cnto})")
    print(f"  → HTTP e2e p99 Δ: L1 on 省 {p99o - p99:.0f}µs ({p99o / p99:.0f}x)")
    await client.aclose()
    await rm.close_redis()


if __name__ == "__main__":
    asyncio.run(main())
