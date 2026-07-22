# Latency Hardening Design (pipelining + L1)

> Date: 2026-07-22 · Round: R3e · Branch: `fix/r3e-latency-hardening`
> Status: design (pre-implementation)
> Defer origin: R3d sidecar spike 结论（§9-C：残余 2 Redis RTT 用 pipelining+dispatcher L1 消减，非 sidecar）

## 1. 背景与目标

R3d 评估 auth/quota sidecar 化时确认：残余延迟是 **2 个串行 Redis RTT/请求**（身份缓存 +
resolver 快照），用本设计消减。本设计是**分析驱动**（R3d 的 RTT 分析），非实测驱动——
实现时顺带加结构化延迟埋点，便于回归验证（见 §9）。

**目标**：把 dispatcher 热路径（bearer+APISIX，占绝大多数流量）的每请求 Redis RTT 从 2
降到 0（L1 命中）/ 1（L1 miss 后 pipelined 回填）；HMAC 暖路径从 3 降到 1（nonce SETNX
不可缓存，留 1）。**不**以牺牲 R2e HMAC 安全语义为代价。

**非目标**：跨进程强一致缓存（pub/sub 逐出）；auth/quota sidecar（R3d 已否）；改 APISIX
edge key-auth Lua（已在 edge，无热路径 caller）；实测基准化（本轮加埋点，正式基准留下轮）。

## 2. 当前热路径延迟分析

**Bearer + APISIX（主路径）** —— 2 串行 RTT：
1. middleware `authenticate_request` → X-Ingress-Auth 快路径 → `identity.read_identity(api_key)`
   = 1 Redis GET（`ak:{sha256}`）。
2. route handler `resolver.resolve_by_header(version_id)` → `redis.t_get(snapshot:{version_id})`
   = 1 Redis GET（TTL 300s）。
   - 两读**独立**（key 分别是 api_key 与 version_id header），但当前分处 apihub_core
     （auth）与 dispatcher（resolver），串行执行。

**HMAC（post-R2e）** —— 最多 3 RTT：
1. `identity.read_identity`（identity GET，miss 时 +1 httpx auth verify）。
2. `identity.read_hmac_secret`（hmac_secret GET）。
3. nonce SETNX（1 RTT，不可缓存——重放防护须每请求写）。
   - 1、2 同在 `_verify_hmac` 内，可 pipeline；3 必须留。

**httpx**：`_verify_via_auth_service`（bearer 冷回源）与 HMAC 冷路径每调用 `async with
httpx.AsyncClient()`——**无连接复用**；dispatcher 转发已用共享池（`main.py`）。冷路径罕见
（identity 缓存命中率 95%+），但零成本顺手修。

## 3. 设计

### 3.1 L1 in-process 缓存（主 lever）

**落点**：dispatcher 进程内。`apihub_core` 提供通用 `l1.TTLCache`（纯数据结构，无副作用）
+ `identity` 的 **opt-in L1 hook**（默认关）；dispatcher 在 lifespan 注入自己拥有的
`TTLCache` 实例。其他服务不注入 → identity 行为不变（blast radius 锁在 dispatcher 接线）。

**TTL**：5s（固定，env `DISPATCHER_L1_TTL_SECONDS=5` 可调）。短 TTL 收口陈旧窗（见 §7）。

**缓存内容（数据，非决策）**：
- identity：`identity.read_identity` 的返回 dict（`{is_active, tenant_id, ..., hmac_enrolled,
  key_id}`）per api_key。命中即返，miss → Redis → 回填 L1（TTL 5s）。
- hmac_secret：`identity.read_hmac_secret` 的加密 blob per api_key（同上）。
- snapshot：`resolver.resolve_by_header` 的 snapshot per version_id（dispatcher 自有，直接接线）。

**契约**：L1 只缓存 Redis 的值；**鉴权决策（enrolled 校验 / HMAC verify / nonce / replay）
每请求仍全跑**。L1 命中 ≠ 跳过验签，只是省掉读 Redis。

### 3.2 Pipelining（次 lever，仅同函数内的读）

**HMAC 暖路径**：`_verify_hmac` 把 identity + hmac_secret 两读合并成 1 个 Redis pipeline
（`raw_client().pipeline().get(k_identity).get(k_secret).execute()`），**投机取 secret**，
enrolled 校验后才用（unenrolled key 的 secret key 在 Redis 不存在，浪费无害）。2→1 RTT。

**Bearer 路径（identity + snapshot）**：跨 apihub_core/dispatcher 边界，pipelining 须把
snapshot 读挪进 middleware（重构 resolver 契约）。鉴于 L1 已把命中压到 0 RTT，且 miss 仅
~1/5s/key（罕见），**判 YAGNI，defer**。spec 记录，留作数据驱动再启。

### 3.3 httpx 池化（tertiary，零风险）

`apihub_core.auth` 改用**进程级共享 `httpx.AsyncClient`**（lazy 单例，lifespan 关闭）替代
每次 `async with`。`_verify_via_auth_service` 与 HMAC 冷路径共用。仅 cache-miss 命中，
收益小但零正确性风险、零行为变化。

## 4. 组件与接口

| 文件 | 责任 | 动作 |
|---|---|---|
| `apihub_core/l1.py` | 通用 TTL 缓存（size-bounded LRU + 过期） | Create |
| `apihub_core/identity.py` | identity/hmac_secret 读加 opt-in L1 hook + pipeline 读 | Modify |
| `apihub_core/auth.py` | `_verify_hmac` 用 pipeline 读；共享 httpx client | Modify |
| `dispatcher/main.py` | lifespan 注入 L1（identity + secret）+ 共享 httpx | Modify |
| `dispatcher/resolver.py` | snapshot L1（自有 TTLCache） | Modify |
| `apihub_core/config.py` | `dispatcher_l1_ttl_seconds=5` 等 Settings | Modify |
| 测试 | L1 命中/miss/过期/逐出；pipeline 读等价；httpx 复用 | Create |

**`apihub_core/l1.py` 接口**：
```python
class TTLCache:
    def __init__(self, maxsize: int = 4096, ttl: float = 5.0): ...
    def get(self, key: str) -> object | None: ...        # 过期返 None + 惰性淘汰
    def set(self, key: str, value: object) -> None: ...   # 用构造 ttl
    def invalidate(self, key: str) -> None: ...
    def clear(self) -> None: ...
```
单 asyncio 事件循环 → 无锁（协作式）；`maxsize` 防 unbounded。

**`identity` opt-in hook**：
```python
_identity_l1: TTLCache | None = None
_secret_l1: TTLCache | None = None
def configure_l1(*, identity: TTLCache | None = None, secret: TTLCache | None = None) -> None: ...
# read_identity/read_hmac_secret：L1 configured 则先查 L1；miss→Redis→回填。
# delete_identity/delete_hmac_secret：同时 invalidate L1（同进程逐出，跨进程靠 TTL）。
async def read_identity_and_hmac_secret(api_key) -> tuple[dict|None, str|None]:  # pipeline 版
    ...
```

## 5. 数据流

**Bearer warm（L1 命中）**：middleware → identity L1 hit（0 RTT）→ ctx → handler → snapshot
L1 hit（0 RTT）→ forward。**0 Redis RTT**。

**Bearer warm（L1 miss）**：identity L1 miss → Redis GET + 回填 L1；snapshot L1 miss → Redis
GET + 回填。**2 RTT**（罕见，~1/5s/key；bearer pipelining defer 故不合并）。

**HMAC warm（L1 命中）**：identity+secret 经 pipeline 读（L1 双命中 → 0 RTT）→ enrolled 校验
→ timestamp → nonce SETNX（1 RTT）→ verify。**1 RTT**（nonce）。

**HMAC warm（L1 miss）**：pipeline MGET identity+secret（1 RTT）+ 回填 L1 → nonce（1 RTT）→
verify。**2 RTT**（罕见）。

**冷启动/Redis 故障**：L1 空 → 落 Redis；Redis 故障 → 现有 503/降级不变（L1 不引入新故障路径，
只是 Redis 前置一层）。

## 6. 错误处理

- **L1 损坏/类型错**：identity.read_identity 已有 Redis miss→清除逻辑；L1 命中但值损坏（非
  dict）→ 视同 miss，删 L1 条目，落 Redis。
- **Redis pipeline 失败**：pipeline 抛异常 → 现有 `_verify_via_auth_service` 503 路径不变。
- **httpx 共享 client 关闭后访问**：lifespan 关闭后不应有在飞请求（FastAPI lifespan 保证）；
  lazy 单例遇 closed → 重建（防御）。
- **TTLCache OOM**：maxsize LRU 淘汰，不增内存压力。

## 7. 正确性与陈旧窗

- **L1 缓存数据非决策**：enrolled 校验、HMAC verify、nonce SETNX、replay、timestamp 每请求
  全跑——L1 不影响 R2e 安全语义。
- **revoke/rotate 跨进程陈旧**：revoke 在 **auth** 服务进程执行（`invalidate` 删 Redis），不
  能逐出 **dispatcher** 进程的 L1 → dispatcher L1 命中时，已 revoke/rotate 的 key 最多 **5s**
  内仍放行（TTL 到期自愈）。**已接受**（§决策：短 TTL 接受陈旧）。同进程 `delete_*` 仍逐出
  L1（auth 自身一致性）。
- **retire 陈旧**：resolver snapshot L1 命中时，retire 后最多 5s 仍路由（现有 Redis 缓存已
  有 5min 同类窗，L1 缩短之）。resolver 的 status 校验保留。
- **identity 字段漂移**：R2e 给 identity 加了 `hmac_enrolled`/`key_id`；L1 缓存整个 dict，
  老 L1 条目（跨版本）可能缺字段 → `.get(..., default)` 防御（现有代码已用 `.get`）。

## 8. 配置

`apihub_core.config.Settings` 新增（均带安全默认）：
- `dispatcher_l1_ttl_seconds: float = 5.0`
- `dispatcher_l1_maxsize: int = 4096`
- `dispatcher_l1_enabled: bool = True`（一键关，回退纯 Redis）

dispatcher lifespan 读 Settings 建 TTLCache 并 `identity.configure_l1(...)`；resolver 自建。

## 9. 测试

- `l1.TTLCache`：get/set/过期（sleep）/LRU 淘汰/invalidate/clear。
- identity L1：configure 后命中返值、miss 落 Redis 回填、delete 同进程逐出、未 configure
  时行为不变（回归）。
- `read_identity_and_hmac_secret` pipeline：返 (dict, blob)，与分两次读等价；unenrolled key
  secret 为 None。
- resolver snapshot L1：命中/miss/retire 逐出。
- `_verify_hmac`：pipeline 读路径回归（R2e 7 测不改向，新增 L1-hit/miss 两测）。
- httpx：共享 client 复用（mock 计数 `AsyncClient` 构造 ==1）。
- 埋点（structlog timing）：`identity_l1_hit`/`snapshot_l1_hit`/`auth_redis_ms` 计数+计时，
  便于 §1 的回归验证（无 assert，仅观测）。

## 10. 部署

- 纯代码改动 + env（默认开）；无 schema/migration；无新外部依赖（`l1.py` 纯 stdlib）。
- 一键关 `DISPATCHER_L1_ENABLED=false` 回退现状（应急）。
- 多副本/多区：L1 每副本独立，陈旧窗 5s 各自收敛（与 Redis 缓存语义同级）。

## 11. 风险

- **陈旧放行 5s**：revoke/rotate 后窗口。已接受；若后续需强一致，留 pub/sub 逐出（本轮不做）。
- **L1 命中率不达预期**：若热 key 基数大 / 5s 内去重不足，收益小。埋点回归验证；TTL 可调。
- **pipeline 读改 identity 内部**：reviewer 须确认 pipeline 与分次读在 L1 miss 语义等价
  （secret 投机取无害）。
