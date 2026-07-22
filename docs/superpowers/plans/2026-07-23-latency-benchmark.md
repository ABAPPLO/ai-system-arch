# Latency Benchmark Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: subagent-driven-development or executing-plans。本轮为**测量轮**,倾向 inline 执行(对真 Redis 连接/seed/负载跑数需紧控制,边跑边调)。

**Goal:** 实测 R3e 延迟收益——微基准(component × L1 on/off × 命中态,真 Redis)+ e2e(HTTP P50/P99 L1 on/off),结果入 `docs/latency-benchmark-results.md`。

**Tech Stack:** Python 3.11 / stdlib `time.perf_counter_ns`(无新 dep)/ 真 apihub-redis(:16380)/ dispatcher uvicorn :8001。

**Spec:** `docs/superpowers/specs/2026-07-23-latency-benchmark-design.md`

## Global Constraints

- **不改 R3e 代码**(只测;`identity.configure_l1` / `DISPATCHER_L1_ENABLED` 是现成开关)。
- 微基准**打真 Redis**(`redis://:apihub_dev_pwd@localhost:16380/0`),非 fakeredis(否则无 RTT 信号)。
- 无新依赖(纯 stdlib harness)。
- 结果文档化 + 微基准可复现(`make bench`)。

---

## File Structure

| 文件 | 责任 | 动作 |
|---|---|---|
| `benchmarks/_stats.py` | 统计 helper(warmup + N 轮 + min/median/p50/p99/mean) | Create |
| `benchmarks/bench_identity.py` | 微基准:identity/resolver × L1 on/off × 命中态 | Create |
| `benchmarks/e2e_load.py` | e2e:本地 dispatcher + mock backend + 负载 P50/P99 | Create |
| `docs/latency-benchmark-results.md` | 实测结果表 + 结论 | Create |
| `Makefile` | `make bench` 目标 | Modify |

---

## Task 1: 微基准 harness + 跑 + 结果

**Files:** `benchmarks/_stats.py`, `benchmarks/bench_identity.py`

**Interfaces:** 用 `apihub_core.identity`(configure_l1/read_identity/read_identity_and_hmac_secret/write_identity)、`apihub_core.redis`(init 或直连)、`dispatcher.resolver`(configure_snapshot_l1/resolve_by_header)。

- [ ] **Step 1: `benchmarks/_stats.py`** — stdlib 统计:
```python
import statistics, time

async def bench(fn, *, n_warmup=200, n=2000):
    import asyncio
    for _ in range(n_warmup):
        await fn() if asyncio.iscoroutinefunction(fn) else fn()
    xs = []
    for _ in range(n):
        t = time.perf_counter_ns()
        await fn() if asyncio.iscoroutinefunction(fn) else fn()
        xs.append(time.perf_counter_ns() - t)
    xs.sort()
    def pct(p): return xs[min(len(xs) - 1, int(len(xs) * p))]
    return {
        "n": n, "min_us": xs[0] / 1e3, "p50_us": pct(0.5) / 1e3,
        "p99_us": pct(0.99) / 1e3, "mean_us": statistics.mean(xs) / 1e3,
    }

def row(label, r):  # 打印一行
    print(f"{label:<48} min={r['min_us']:8.2f}  p50={r['p50_us']:8.2f}  p99={r['p99_us']:8.2f}  mean={r['mean_us']:8.2f} µs")
```

- [ ] **Step 2: `benchmarks/bench_identity.py`** — 连真 Redis,seed,跑矩阵:
```python
import asyncio, os
os.environ.setdefault("REDIS_HOST", "localhost"); os.environ.setdefault("REDIS_PORT", "16380")
os.environ.setdefault("REDIS_PASSWORD", "apihub_dev_pwd")
os.environ.setdefault("PG_HOST","x"); os.environ.setdefault("PG_USER","x"); os.environ.setdefault("PG_PASSWORD","x")

from apihub_core import identity, redis as redis_mod
from apihub_core.l1 import TTLCache
from benchmarks._stats import bench, row

APIKEY = "ak_bench_aaaaaaaaaaaaaaaa"

async def main():
    await redis_mod.init_redis(...)  # 用 settings;或直连 raw client
    await identity.write_identity(APIKEY, {"is_active":True,"tenant_id":"t","tenant_type":"internal","app_id":"a","key_id":"k","hmac_enrolled":False}, ttl=600)
    # A: read_identity
    identity.configure_l1(identity=TTLCache(ttl=60), secret=TTLCache(ttl=60))
    identity._identity_l1.set(APIKEY, {...})  # L1 hit 态
    row("read_identity L1=on hit", await bench(lambda: identity.read_identity(APIKEY)))
    identity._identity_l1.clear()  # miss→Redis
    row("read_identity L1=on miss(Redis)", await bench(lambda: identity.read_identity(APIKEY)))
    identity.configure_l1(identity=None, secret=None)  # L1 off
    row("read_identity L1=off", await bench(lambda: identity.read_identity(APIKEY)))
    # B: read_identity_and_hmac_secret (pipeline) — both-hit / both-miss / off
    # C: resolver.resolve_by_header — snapshot hit/miss/off(需 seed snapshot 到 Redis)
    ...
asyncio.run(main())
```
> 实现时:用 `apihub_core.config.get_settings()` 取 Redis 配置调 `redis_mod.init_redis`;resolver snapshot seed 用 `redis.t_set("snapshot:<v>", json.dumps(asdict(snap)))`。命中态/miss 态用 `_l1.set`/`clear` + Redis set 控制。

- [ ] **Step 3: 跑 + 收数**:`.venv/bin/python -m benchmarks.bench_identity`(或 `PYTHONPATH=. .venv/bin/python benchmarks/bench_identity.py`)。记录 µs 表。

- [ ] **Step 4: 写 `docs/latency-benchmark-results.md`** 微基准节(表 + delta + 复现命令)。

- [ ] **Step 5: Commit**:`r3i T1: 微基准 harness + 实测结果(identity/resolver × L1 on/off)`。

---

## Task 2: e2e HTTP 负载 + 结果

**Files:** `benchmarks/e2e_load.py`

- [ ] **Step 1: 起 mock backend**(本地 echo httpx/uvicorn server,返 200 `{}`),记录端口。
- [ ] **Step 2: 起 dispatcher**(`.venv/bin/uvicorn dispatcher.main:app --port 8001`,env: PG/Redis 指 dev、`DISPATCHER_L1_ENABLED=true`、resolver 指向 mock backend url via seed snapshot)。seed identity + snapshot 到 Redis。
- [ ] **Step 3: 负载脚本** `e2e_load.py`:asyncio N=20 并发 × M=2000 `POST /dispatch/{path}`(X-API-Key + X-API-Version-Id),收每请求 `perf_counter`,算 P50/P99。
- [ ] **Step 4: L1 on vs off** — 重启 dispatcher(`DISPATCHER_L1_ENABLED=false`)重跑,得两组 P50/P99。
- [ ] **Step 5: 兜底** — 若 /dispatch 全链(auth X-API-Key 经 APISIX? 或本地 middleware 回源 auth-svc)接线不稳,降级为直接循环调 `authenticate_request(req, settings, api_key)` + `resolve_by_header(v)` 的「component-HTTP」P50/P99(仍真 Redis、L1 on/off)。
- [ ] **Step 6: 写** e2e 节到 results 文档。
- [ ] **Step 7: Commit**:`r3i T2: e2e HTTP 负载 P50/P99(L1 on/off)`。

---

## Task 3: `make bench` + 收尾

- [ ] **Step 1: Makefile** 加 `bench:` 目标 → `PYTHONPATH=. .venv/bin/python -m benchmarks.bench_identity`(微基准,可入 CI optional;e2e 手动跑)。
- [ ] **Step 2: 结果文档** 终稿(结论:R3e 每请求实测省多少 µs/RTT;本地为下界)。
- [ ] **Step 3: 回归**:`ruff`(0.6.x CI 版)+ 关键 suite 不回归(benchmarks 不入单测)。
- [ ] **Step 4: PR + review**(轻量;测量轮无安全/逻辑风险,review 聚焦 harness 正确性 + 结果可信度)。

---

## Self-Review

**Spec coverage:** §4 微基准 → T1 ✓;§5 e2e → T2(含降级)✓;§6 结果文档 → T1/T2 写入 ✓;§7 CI(`make bench`) → T3 ✓;§8 风险(真 Redis、e2e 接线、本地 RTT 下界) → 文档标注 + 兜底 ✓。

**Placeholder:** T1 Step 2 的 seed/命中态控制 + resolver snapshot seed 给了骨架,实跑时按真模块接口填(`init_redis` 签名、`t_set`);T2 负载参数 N/M 可按实跑调。无静态 TBD。

**Type consistency:** `bench()` 返回 dict、`row()` 打印 — 跨 Task 一致。
