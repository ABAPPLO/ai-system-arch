# 正式 Latency Benchmark 设计（microbench + e2e）

> Date: 2026-07-23 · Round: r3i · Branch: `fix/r3i-latency-benchmark`
> Status: design
> 目的:用可重复的正式基准**实测** R3e 的延迟收益(L1 命中消除 Redis RTT + HMAC pipeline),把「分析驱动 2→0/1 RTT」变成「实测数字」。

## 1. 目标

两组基准,均对比 **L1 开 vs 关**(`DISPATCHER_L1_ENABLED` / `identity.configure_l1`):

1. **微基准(component,入 CI)**:针对真 apihub-redis,测 `identity.read_identity` / `read_identity_and_hmac_secret`(pipeline)/ `resolver.resolve_by_header` 的每调用延迟,L1 hit/miss/cold × L1 on/off。量化 R3e 的 Redis-RTT 收益。
2. **e2e(HTTP 负载)**:本地起 dispatcher(:8001,接 apihub-redis + mock backend),异步负载 N 并发 × M 请求打 `/dispatch/...`,测 P50/P99,L1 on vs off。量化真实路径收益。

## 2. 非目标

- 不改 R3e 代码(只测)。
- 不做线上生产压测(仅本地 dev 栈:apihub-redis/kind)。
- 微基准不引入重 dep(用 stdlib `time.perf_counter` + 自写统计:warmup + N 轮 + min/median/p50/p99/mean;不加 pytest-benchmark,免新依赖)。

## 3. 环境

- 真 Redis:`redis://:apihub_dev_pwd@localhost:16380/0`(docker `apihub-redis`)。
- dispatcher 本地:`.venv/bin/uvicorn dispatcher.main:app --port 8001`(需 PG/Redis env + seed 身份/snapshot;e2e 用)。
- Python 3.11 `.venv`。

## 4. 微基准设计(`benchmarks/bench_identity.py`)

纯 stdlib harness(无 dep):
```python
# 伪码
async def bench(fn, n_warmup=200, n=2000):
    for _ in range(n_warmup): await fn()
    xs=[]; 
    for _ in range(n):
        t=time.perf_counter_ns(); await fn(); xs.append(time.perf_counter_ns()-t)
    return stats(xs)  # min/median/p50/p99/mean in µs
```
场景(每个 L1 on/off 各跑):
- A `read_identity(api_key)`:L1 hit(seed L1)/ L1 miss(清 L1,Redis 命中)/ cold(Redis miss)。
- B `read_identity_and_hmac_secret(api_key)`:L1 both-hit / both-miss(1 pipeline RTT 取 2 key)/ cold。
- C `resolver.resolve_by_header(version_id)`:snapshot L1 hit / miss(Redis 命中)。
输出:控制台表 + 写 `docs/latency-benchmark-results.md`(µs + L1 on/off delta + 命中态)。

**期望**:L1 on 命中态 ≈ in-process dict lookup(亚 µs);L1 off 或 miss = 真 Redis GET RTT(本地 docker 约 0.1–0.5ms)。delta = R3e 每请求省下的 RTT。

## 5. e2e 负载设计(`benchmarks/e2e_load.py`)

- 起 dispatcher(:8001,`DISPATCHER_L1_ENABLED=true`),seed 一把真 identity(`write_identity`) + api_key + resolver snapshot 到 apihub-redis;mock backend(本地 echo `httpx` server)作 forward 目标。
- 负载:`asyncio.gather` N=20 并发 × M=2000 请求 `POST /dispatch/{path}`(带 X-API-Key + X-API-Version-Id),收每请求 `perf_counter` 延迟。
- 跑两遍:L1 on vs off(env 切换重启 dispatcher),得各自 P50/P99。
- 兜底:若 /dispatch 全链(auth+resolver+forward)接线过重/不稳,降级为「middleware+resolver 直接调 `authenticate_request`+`resolve_by_header` 的 component-HTTP 替代」(仍真 Redis、L1 on/off、出 P50/P99)。
输出:P50/P99 表 + 并入 results 文档。

## 6. 结果文档(`docs/latency-benchmark-results.md`)

- 微基准表(component × L1 on/off × 命中态,µs)+ e2e P50/P99(L1 on/off)。
- 结论:R3e 每请求实测省下多少 RTT/µs;L1 命中率假设下的净收益。
- 复现命令 + 运行环境(redis 版本、docker 本地)。

## 7. CI 集成

- 微基准 `benchmarks/bench_identity.py` 可入 CI(optional job,`continue-on-error`,因依赖真 Redis——或 CI 用 service container redis)。e2e 不入 CI(本地/一次性)。
- 标 `@pytest.mark.benchmark` 或独立 `make bench` 目标,与单测分离(不拖慢单测)。

## 8. 风险

- **fakeredis ≠ 真 RTT**:故微基准必须打真 apihub-redis(本地 docker RTT 真实,虽比生产网络低,但 L1 on/off delta 仍可信)。
- **e2e 接线复杂**:auth(api_key 校验)+ resolver(snapshot)+ forward(mock)任一不稳即拖;§5 兜底降级 component-HTTP。
- **本地 docker Redis RTT 偏低**(同宿主):生产跨网 RTT 更高 → L1 收益更大;文档标注「本地数字为下界」。
