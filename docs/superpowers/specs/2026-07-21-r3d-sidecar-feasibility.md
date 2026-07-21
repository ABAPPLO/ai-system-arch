# R3d 同步扇出 sidecar 可行性评估（spike）

> 日期：2026-07-21
> 阶段：APIHub fix-program · Wave 3 · R3d（**仅评估，不实施**）
> 关联：[phase4 审计](../../phase4-audit-findings.md) §9.2(C)、[fix-program spec](2026-07-15-apihub-fix-program-design.md) §7 R3d、[R1d](2026-07-17-r1d-apisix-auth-design.md)（Redis 身份缓存）、[R3a](2026-07-18-r3a-go-quota-design.md)（Go quota Redis Lua）、`docs/06-high-concurrency.md`
> base：main=`13ff6b2`（R3c #62 合后）
> 依赖：R1c + R1d + R3a

## 1. §9-C 原始张力（审计 phase4 §9.2-C）

> 每请求同步扇出：网关 → auth → quota → dispatcher → 后端，4 跳串行 HTTP，目标 P99 < 200ms。auth/quota 靠缓存撑，但架构把延迟税 baked-in；**若真要 5w QPS，auth/quota 更该是 sidecar**（Envoy ext-auth / Lua 插件）。Go 重写 quota 是治标。

R3d 任务：评估 auth/quota sidecar 化是否值得；**若不可行，§9-C 标「已知接受」**。

## 2. 现状核实（post-R1d/R3a）—— 4 跳串行 HTTP 已不成立

逐跳追踪真实 sync 请求路径（file:line 证据）：

| 跳 | 类型 | 位置 | 证据 |
|---|---|---|---|
| client → APISIX | HTTP | edge | ingress |
| APISIX `key-auth`（consumer 校验） | **in-process Lua** | edge | `apisix_client.py:71` 每 published route 带 key-auth |
| APISIX `limit-count`（限流门，可选 per-route） | **in-process Lua** | edge | `apisix_client.py:80-87`（`policy: "local"`） |
| APISIX → dispatcher | HTTP | `proxy-rewrite` 注入 `X-API-Version-Id`+`X-Ingress-Auth` | `apisix_client.py:66-68` |
| dispatcher: Redis 身份缓存 GET | Redis（1 RTT） | `auth.py:44` `identity.read_identity`（R1d trust-path，`X-Ingress-Auth` 命中→免 HTTP 回源） | `auth.py:36-55` |
| dispatcher: Redis resolver 快照 GET | Redis（1 RTT） | `routes.py:73` `resolve_by_header` | `dispatcher/resolver.py` |
| dispatcher → backend | HTTP | `forwarder.py:71` `httpx` | |

**实际内部串行链 = 2 HTTP 跳（APISIX→dispatcher, dispatcher→backend）+ 2 Redis RTT（身份缓存 + resolver 快照，均在 dispatcher 进程内）。** 不是 4 跳串行 HTTP。

### auth HTTP 跳：已被 R1d 消除
- edge `key-auth`（APISIX Lua，in-process）+ R1d Redis 身份缓存：dispatcher 读 Redis 重建 `TenantContext`（`auth.py:44-55`），**warm cache 下无 httpx 调用**。auth-svc HTTP 仅 cache-miss/冷启触发（`auth.py:81-94`，timeout 5s）。缓存写于 key create/revoke 生命周期（`auth/routes.py:182` `identity.write_identity`；`auth/cache.py:22-29` TTL 5min pos/1min neg）。→ **auth HTTP 跳在 warm path 上不成立**。

### quota HTTP 跳：从未接入 sync 请求路径
- `grep -rln "quota" services/services/{executor,workflow,ai-gateway,dispatcher}/src` → **零命中**（排除 tests/egg-info）。dispatcher `dispatch()`（`routes.py:60-106`）resolve→visibility→forward，无 quota 调用。
- 每请求限流 = APISIX `limit-count`（in-process Lua at edge，`apisix_client.py:80-87`），**非 HTTP 跳到 quota-svc**。
- Go quota 服务存在且加固（R3a：Redis Lua Eval 1 RTT `limiter/redis.go:254`；R1d trust-ingress `cmd/main.go:55-59`），但**热路径无 caller**（仅 smoke test / portal admin usage 读 / quota 自身）。
- → **quota HTTP 跳在 §9-C 4 跳里当前不发生**。

### 残余延迟税
R1d/R3a 未消减的：(a) APISIX→dispatcher HTTP 跳；(b) dispatcher→backend HTTP 跳；(c) **dispatcher 内 2 Redis RTT**（身份缓存 GET + resolver 快照 GET）。当前串行成本的实质——比审计的「4 串行 HTTP」小得多，且 auth/quota 功能（key-auth + limit-count）**已在 edge**（APISIX Lua 插件）。

### 延迟目标（`docs/06-high-concurrency.md`）
- P99：simple sync（backend<100ms）= **200ms**（§9-C 目标）；simple read（纯缓存）= 80ms。
- timeout budget：APISIX 30s / dispatcher 5s / **auth-svc 200ms / quota-svc 100ms** / Redis 100ms。注：auth-svc 200ms 是 **cache-miss fallback** 预算，非 steady-state 税。
- QPS 愿景：10w（APISIX 5-8w / auth 1w cache-hit / quota 1-2w / dispatcher 5k-1w per pod）。Redis hit-rate SLO >95%。

## 3. Sidecar 选项 + 延迟/复杂度（web 调研）

| 选项 | 每请求开销 | 网络依赖 | 部署复杂度 | 何时选 |
|---|---|---|---|---|
| **Envoy `ext_authz`（gRPC 外部鉴权）** | ~1–5ms（localhost gRPC + 序列化 + 上下文切换） | 是（外部 svc） | 高（独立 svc 部署/监控/扩容） | 跨多 proxy 共享策略 / 非 Lua 复杂逻辑 / 严格决策-执行分离 |
| **APISIX in-process Lua 插件** | ~0.1–0.5ms（LuaJIT，无网络跳、无序列化） | 否（进程内） | 低（单进程） | 延迟临界（<5ms p99 预算）/ 逻辑直接（JWT/API-key/限流） |
| **Istio sidecar** | ~2–10ms（依实现+位置） | 是 | 高（mesh 全套） | 已有 mesh / 跨服务统一 mTLS+策略 |

in-process 插件比外部 auth 调用快 **5–20×**。ext_authz 的 gRPC 跳本身 ~1-5ms——比当前 APISIX Lua key-auth（~0.1-0.5ms）**慢一个量级**。

## 4. 分析

### sidecar 会 ADD 延迟 + 复杂度，而非减
当前 auth/quota 已是 **APISIX in-process Lua**（key-auth + limit-count，~0.1-0.5ms，无网络跳）。换成 sidecar（Envoy ext_authz）：
- 每请求多 ~1-5ms gRPC 跳（localhost），**比现状慢一个量级**；
- 多一个独立 svc 部署/监控/扩容 + gRPC 连接池管理 + protobuf 序列化；
- 失败模式：auth svc 挂→请求 fail（或 fail-open/closed 配置）——当前 in-process 无此依赖。
→ **sidecar 对 auth/quota 不是延迟优化，是延迟退化 + 复杂度增加**。审计 §9-C 的「sidecar」建议基于 pre-R1d 的「4 跳 HTTP」前提，该前提已过时。

### 残余税（2 Redis RTT）的消减不需 sidecar
残余串行成本是 dispatcher 内 2 Redis GET（身份缓存 + resolver 快照）。消减路径（无 sidecar pivot）：
1. **Redis pipelining**：batch 2 GET 成 1 RTT（dispatcher 进程内，`redis.py` 已是 async client，加 pipeline 即可）——最小改动，~½ RTT。
2. **dispatcher L1 in-process cache**：身份缓存 + resolver 快照上叠一层进程内 TTL cache（TTL 5-30s），warm 下 0 RTT——命中率高（identity 变更低频，resolver 快照按 version_id 稳定）。
3. **edge co-location（激进，不推荐）**：把 2 Redis GET 移进 APISIX Lua 插件——破坏 dispatcher resolve 职责归属 + 业务逻辑塞进 gateway，违背 R1c 路由归属边界。

1+2 是低风险增量优化，不需 sidecar。

### 5w QPS 触发点再评估
审计「若真要 5w QPS」触发：当前 QPS 愿景 10w（APISIX 5-8w）。5w 量级下：
- APISIX in-process Lua（key-auth + limit-count）单 pod 可承载（Lua 子毫秒）；
- 瓶颈更可能在 **2 Redis RTT 串行** + dispatcher httpx 连接池——用 pipelining + L1 + 连接池调优解决，**不需 sidecar**；
- 真正可能需 sidecar 的场景：跨多 proxy 共享复杂策略 / 非 Lua 鉴权逻辑（如 OAuth2 复杂流程）——当前 APIHub 是 API-key + key-auth，逻辑简单，不在此列。

## 5. 建议

**R3d 结论：auth/quota sidecar 化不值得——§9-C 标「已知接受」。**

理由：
1. **§9-C 前提过时**：post-R1d/R3a，实际链是 2 HTTP + 2 Redis RTT，非 4 串行 HTTP。auth HTTP 跳已被 R1d 消除（edge Lua + Redis 身份缓存），quota HTTP 跳从未接入热路径（edge limit-count in-process）。
2. **sidecar 是延迟退化**：Envoy ext_authz ~1-5ms gRPC 比当前 APISIX in-process Lua ~0.1-0.5ms 慢一个量级 + 加部署/监控/扩容复杂度。
3. **残余税有更优解**：2 Redis RTT 用 pipelining + dispatcher L1 消减（低风险增量），不需 sidecar pivot。
4. **auth/quota 已在 edge**：key-auth + limit-count 是 APISIX Lua 插件——sidecar 要 host 的功能已在最优位置（in-process at edge）。

**§9-C 张力 → 已知接受**（R1d/R3a 已吸收原始张力；残余 2 Redis RTT 用 pipelining/L1 优化，非 sidecar；future 触发：若真 5w+ QPS 且 2 Redis RTT 成瓶颈，优先 pipelining+L1+连接池，再考虑 edge co-location，sidecar 是最后选项）。

**defer 跟进项（非本轮，记录）**：
- dispatcher Redis pipelining（2 GET→1 RTT）——小改，高 ROI。
- dispatcher L1 in-process cache（identity + resolver snapshot，TTL 5-30s）——中改。
- dispatcher httpx 连接池调优——R2c 数据面已做部分。
- 这些是 latency hardening 下一轮候选，与 sidecar 无关。

## 6. 来源（web 调研）
- Envoy `ext_authz` filter 文档（gRPC 同步外部鉴权 + ~1-5ms localhost 开销）。
- APISIX 文档 + blog（in-process Lua 插件 + ~0.1-0.5ms）。
- 社区 benchmark（in-process 插件比外部 auth 调用快 5-20×）。
- Istio `ext_authz` 讨论（~2-10ms 依实现）。
（数值依部署拓扑/ext_authz svc 位置/TLS/逻辑复杂度而异；量级结论稳定：in-process 远快于 external。）

## 7. 参考
- [phase4 审计](../../phase4-audit-findings.md) §9.2(C) / §9.3
- [fix-program spec](2026-07-15-apihub-fix-program-design.md) §7 R3d
- [R1d spec](2026-07-17-r1d-apisix-auth-design.md) / [R3a spec](2026-07-18-r3a-go-quota-design.md)
- `docs/06-high-concurrency.md`（P99 + timeout budget + QPS 拆分）
- 关联记忆：[[apihub-fix-program-progress]] [[apihub-audit-2026-07-15]]
