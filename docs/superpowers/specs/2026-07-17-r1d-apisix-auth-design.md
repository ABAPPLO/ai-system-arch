# R1d spec — APISIX 鉴权闭环（consumer 管理 + 发布路由带 key-auth/限流）

日期：2026-07-17 · 分支 `fix/r1d-apisix-auth` · 依据：R1c（#42）review 发现的缺口 + 审计 §9-A（APISIX 应为鉴权+限流+路由层）。

## 问题（R1c review 发现）

R1c 让 APISIX 成为**路由层**（publish_route 下发路由 + 注入 X-API-Version-Id），但**没让它做鉴权/限流**：
- `apisix_client.publish_route` 的路由 payload 只有 `proxy-rewrite`，**没带 `key-auth` / `limit-count`** → 发布的 API 在 APISIX 层不鉴权、不限流。
- 后果：每个请求都打到 dispatcher → auth verify。**(1) 安全/治理**：APISIX 限流层（limit-req/count/conn）对发布 API 失效；**(2) 性能**：dispatcher→auth 冷缓存慢（5s 超时）→ e2e 里 good-key 偶发 **503 "Auth service unreachable"**（R1c review 实测复现）。
- 更深：api-registry **不创建 APISIX consumer**（app→key 映射），consumer-template 只有静态 `app-default`。所以即便给路由加 key-auth，没有对应 consumer 也会全拒——需要 consumer 生命周期管理。

§9-A 的设计意图是 APISIX 做**鉴权 + 限流 + 路由**；R1c 只做了路由。本轮补齐鉴权 + 限流。

## 走法（已定）

**auth 拥有 app/key 聚合 → 负责 APISIX consumer 生命周期；api-registry 拥有 API 元数据 → 负责 APISIX 路由（R1c 已做）。** 二者共享 `apihub_core.apisix_client`（从 api-registry 提到 apihub-core，复用 Admin API 客户端）。

- **APISIX consumer 管理（auth）**：
  - `auth` 创建 APIKey 时（`create_api_key`），同步 `upsert_apisix_consumer(app_id, plaintext_key)`——consumer username=app_id，`key-auth.key`=plaintext。
  - 吊销 key（`revoke_api_key`）→ `delete_apisix_consumer(app_id)`。
  - 这样 APISIX 持有 app→key 映射，能在网关层秒级鉴权（key-auth 走 APISIX 内部缓存，远快于 dispatcher→auth 回源）。
- **发布路由带 key-auth + 限流（api-registry）**：
  - `publish_route` payload 加 `"key-auth":{"header":"X-API-Key"}`（与 consumer-template / §7 一致，显式 header）。
  - 若 `api_version.rate_limit` 有值，加 `"limit-count":{count/time_window/key/policy/rejected_code}`（从 rate_limit 映射）。
- **鉴权下沉到 APISIX 后**：dispatcher 不再每请求 verify（APISIX 已鉴权才转发）；保留 dispatcher 的 visibility/RLS 不变。**这同时修掉 good-key 503 抖动**（APISIX key-auth 缓存命中快，不再冷回源 auth）。

## 改动清单

### ① `apihub_core.apisix_client`（从 api-registry 提到共享库）
- 把 `services/services/api-registry/src/api_registry/apisix_client.py` 迁到 `services/libs/apihub-core/src/apihub_core/apisix_client.py`（保留 `publish_route` / `_admin_request` / `_normalize_path` / `retire_route`）。
- 新增：
  - `upsert_consumer(*, app_id, key) -> None`：`PUT {APISIX_ADMIN_URL}/apisix/admin/consumers/{app_id}`，body `{"username":app_id,"plugins":{"key-auth":{"key":key}}}`。
  - `delete_consumer(app_id) -> None`：`DELETE .../consumers/{app_id}`（不存在静默，404 不当错）。
- api-registry 改 import：`from apihub_core.apisix_client import publish_route`（替换本地模块）。
- 设置：`apisix_admin_url`/`apisix_admin_key` 已在 `apihub_core.config`（R1c 验证过）。

### ② api-registry `publish_route` 加 key-auth + 限流
- `publish_route(*, version_id, method, path, base_path, rate_limit=None)`：plugins 块加 `key-auth`；若 `rate_limit`（dict {count,window_seconds}）非空，加 `limit-count`。
- publish handler 传 `rate_limit=row["rate_limit"]`（api_version 已有该列）。

### ③ auth 接 consumer 生命周期
- `auth/repository.create_api_key` 末尾（或 `auth/routes.create_key` 成功后）：`await apisix_client.upsert_consumer(app_id=app_id, key=plaintext)`。
- `revoke_api_key`：`await apisix_client.delete_consumer(app_id=app_id)`。
- 注意：auth 需连 APISIX Admin（`apisix_admin_url`/`key` 已在 shared config；auth configmap 补 `APISIX_ADMIN_URL`，secret 补 `APISIX_ADMIN_KEY`——R1c review 发现 api-registry-secret 也没这俩，本 round 在 k8s 层补齐 auth + api-registry 的 APISIX_ADMIN_KEY）。

### ④ k8s 配置补齐 APISIX_ADMIN_KEY
- `deploy/k8s/services/api-registry/configmap.yaml` Secret + `auth` configmap Secret 补 `APISIX_ADMIN_KEY`（值走 Sealed Secret；kind 用默认 `edd1c9f...`）。
- 确认 api-registry/auth pod 能跨 ns 访问 `apisix-admin.apihub-ingress:9180`（R1c review 发现 api-registry 连不上——补 NetworkPolicy 或确认 ns 间 DNS 通）。

### ⑤ 文档
- `docs/aggregate-ownership.md`：明确「APISIX consumer 由 auth 管（随 key 生命周期）；路由由 api-registry 管（随 publish）」。

## 验证（走真实入口）
- **单元**：
  - `apisix_client.upsert_consumer/delete_consumer`：stub httpx，断言 PUT/DELETE consumers payload + 404 静默。
  - `publish_route`：断言 payload 含 `key-auth` + 条件 `limit-count`。
  - auth create_key：stub apisix_client，断言 upsert_consumer 被调（app_id + plaintext）；revoke → delete。
- **kind e2e**（APISIX 已部署）：
  1. auth 建 app + key → APISIX consumer 存在（admin GET consumers/{app_id}）。
  2. api-registry publish → 路由含 key-auth。
  3. 经网关：**no-key → 401（在 APISIX，不再到 dispatcher）**；good-key → 200；**连续打 → 触发 limit-count 429**（若有 rate_limit）。
  4. good-key 冷启动不再 503（APISIX key-auth 缓存命中，不回源 auth）——这是 R1c 503 抖动的回归验证。
  5. revoke key → good-key 变 401（consumer 删了）。

## 不做（R1d 边界）
- 不动 dispatcher 的 visibility/RLS。
- 不做多 app 共用 consumer 的复杂 RBAC（一对一 app→consumer 即可）。
- 不补 retire 删 APISIX 路由（仍是 R1c follow-up）。
- 不动 OAuth2/HMAC（auth 那边的独立项）。

## 风险
- **跨 ns 网络**：auth/api-registry → apisix-admin（apihub-ingress ns）需通。R1c review 实测 api-registry 连不上 9180——需确认/补 NetworkPolicy，否则 consumer/route 下发失败。
- **consumer 与 key 一致性**：auth 建 key 是事务，upsert_consumer 是 APISIX HTTP（非事务）——key 建成功但 consumer 失败时，key 在 PG 但 APISIX 不认 → 该 key 经 APISIX 鉴权失败。需 best-effort + 审计日志（或后续对账）。
- **key-auth 缓存**：APISIX key-auth 有内部缓存；revoke 后 consumer 删除，但 APISIX 缓存可能短时仍认旧 key（秒级窗口，可接受；或 revoke 时清 APISIX 缓存——复杂，暂不做）。
- **限流字段映射**：api_version.rate_limit 是 {count, window_seconds}，APISIX limit-count 是 {count, time_window, key, policy}——需映射（policy=redis-cluster? kind 用 local）。

## 步骤（粗，细化交 writing-plans）
1. `apihub_core.apisix_client`（迁移 + upsert/delete_consumer）+ 单测。
2. api-registry publish_route 加 key-auth/limit + 单测。
3. auth create/revoke key 接 consumer + 单测。
4. k8s：auth/api-registry 补 APISIX_ADMIN_KEY + 跨 ns 网络。
5. kind e2e（no-key 401@APISIX / good-key 200 / limit 429 / revoke→401 / 冷启不 503）。
6. commit → 一个 squash-PR。
