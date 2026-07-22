# R3e Latency Benchmark — 实测结果

> Branch `fix/r3i-latency-benchmark` · 2026-07-23 · 方法见 `docs/superpowers/specs/2026-07-23-latency-benchmark-design.md`
> 复现:`make bench`(需 docker `apihub-redis` 在 localhost:16380,pwd `apihub_dev_pwd`)
> 环境:Python 3.11,真 Redis(docker 同宿主,非 fakeredis),`time.perf_counter_ns`,warmup 200 + n 2000(单线程)/ 20 worker × 400(并发)

## 结论(先看)

R3e 的 L1 in-process 缓存把 dispatcher 热路径的 Redis 访问**从每请求 1 RTT 降到 0(L1 命中)**,实测:

| 场景 | L1 on(命中) | L1 off(每请求 Redis) | 收益 |
|---|---|---|---|
| read_identity(单线程 p50) | **2.2 µs** | 118.7 µs | 省 ~117 µs/请求(**98%**) |
| pipeline identity+secret(单线程 p50) | **4.0 µs** | 242.1 µs | 省 ~238 µs/请求(98%) |
| read_identity(20 并发 p99) | **2.2 µs** | **3280 µs** | **1487×**(Redis 连接竞争被 L1 消除) |

**关键发现**:并发下 L1 收益从 98% 飙到 **1487×**(p99)。L1 off 时 20 并发抢 Redis 连接池 → 尾延迟爆炸(p99 3.3ms);L1 on 命中是纯 in-process dict 查找,无连接、无竞争,p99 维持 2µs。**L1 不只省 RTT,更消除了连接池竞争的尾延迟。**

## 详细数据

### A. read_identity(单线程)
| | min | p50 | p99 | mean |
|---|---|---|---|---|
| L1 on hit(dict 查找) | 2.04 | 2.16 | 2.75 | 2.26 µs |
| L1 off(→ Redis GET) | 111.0 | 118.7 | 228.3 | 131.0 µs |

→ L1 命中 p50 **2.2µs** vs Redis **118.7µs**;Δ = **-116.5µs(-98.2%)**。

### B. read_identity_and_hmac_secret(pipeline,2 key)
| | min | p50 | p99 | mean |
|---|---|---|---|---|
| L1 on both-hit(0 RTT) | 3.85 | 4.00 | 5.93 | 4.17 µs |
| L1 off(1 pipeline 取 2 key) | 205.5 | 242.1 | 411.0 | 254.9 µs |

→ HMAC 暖路径(身份+secret 一起取):L1 命中 4µs vs pipeline 242µs;省 ~238µs。
注:本地 docker 下 pipeline(2 key/1 RTT)≈ 2× 单 GET(RTT 极小);**生产跨网 RTT 下 pipeline 比 2 次串行省 1 RTT**,收益更大。

### C. resolver.resolve_by_header(snapshot)
| | min | p50 | p99 | mean |
|---|---|---|---|---|
| L1 on hit(dict) | 2.68 | 2.80 | 3.12 | 2.92 µs |
| L1 off / miss | — | — | — | (需 PG pool,见限制) |

→ snapshot L1 命中 2.8µs(同 identity 量级)。miss 路径 resolve_by_header 在 Redis miss 时回落 DB(meta_db_session),本基准未起 PG,故仅测命中态;语义与 identity 一致(Redis GET ~120µs 量级)。

### D. 并发(20 worker × 400 req)
| | p50 | p99 |
|---|---|---|
| L1 on hit | 1.84 | 2.21 µs |
| L1 off(→ Redis) | 1558.9 | 3280.5 µs |

→ 并发下 L1 off 的 p99 = **3.3ms**(连接池竞争),L1 on 维持 2µs。**1487×**。

## 生产外推

- 本地 docker Redis 同宿主 RTT(~0.1ms)是**下界**;生产跨网 RTT 更高(0.5–2ms),L1 单线程收益随之放大。
- 并发 1487× 的尾延迟收益与网络无关(纯连接池竞争),生产同样成立。
- 假设 L1 命中率 ≥ 80%(热 key + 5s TTL):每请求平均省 (0.8 × 117µs identity + 0.8 × 120µs snapshot) ≈ **~190µs**(单线程),并发尾延迟收益数量级更大。

## 限制 / 未覆盖

1. **C miss 路径**需 PG pool(resolve_by_header Redis-miss 回落 DB);未起 PG,仅测命中态。语义同 identity(Redis GET 量级)。
2. **全 HTTP e2e**(/dispatch 端到端 P50/P99)未跑——component 基准 + 并发已证 L1 收益;HTTP 层只叠加固定 middleware 开销,不改变 L1 delta。留作后续(需 auth-svc/forward mock 全链接线)。
3. 本地 docker Redis RTT 偏低;数字为**下界**。
4. 微基准入 CI(`make bench`,optional);e2e 手动跑。
