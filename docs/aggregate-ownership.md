# 聚合所有权（Aggregate Ownership）

> 硬规则：**BFF（portal / admin）是聚合/转发层，不得直写领域服务的表；跨聚合只能走拥有方 API。**
> 这是 `docs/phase4-audit-findings.md` §9-B「服务/聚合边界泄漏」的架构护栏。多服务共写共读同一批表，是 §2-§4 一堆字段/序列化/ID 漂移集成 bug 的根因。

## 资源 → 归属服务

| 资源 | 归属服务（唯一写权） | 其它服务的访问方式 |
|---|---|---|
| `app` / `api_key` | **auth** | 调 auth API（`/v1/apps`、`/v1/apps/{id}/api-keys`、`/v1/apikey/verify`）；portal 转发用户 JWT |
| `audit_log` / `audit_events` | **admin** | 调 admin API；`admin_db_session` 内部写审计（R0a） |
| `api` / `api_version` | **api-registry** | 调 api-registry API；发布走控制面 |
| `subscription` / `billing_record` | **billing** | 调 billing API |
| `plan` | **billing**（只读可共享） | 只读 |
| quota 计数（Redis `t:{tenant}:...`） | **quota** | 调 quota API |
| 调用日志（ClickHouse） | **trace-svc** 只读聚合 | 通过 trace-svc 查询；CH 无 RLS，强制 tenant 过滤（R3c） |
| `tenant` / `user` 身份 | **auth**（+ tenant-svc 元数据） | 调 auth/tenant API |

## 已修（R0c，2026-07-16）

- portal-bff 的 app/key 自助改走 auth API（`portal/routes.py` 转发，`portal/repository.py` 不再触达 `app`/`api_key` 表）。

## 待推进（按本表硬规则）

- admin 直写 `audit` → 改走 admin 自身 API（后续轮次）。
- quota / billing 从 ClickHouse 读用量算钱 → 明确为 trace-svc 只读聚合的消费者，不直连 CH 写状态。
- 多 Region 写亲和（ADR-013）需尊重本表的区域写权。

## 路由归属（R1c，2026-07-16）

> **路由归属 = APISIX**：
> - **APISIX** 拥有**动态路由** + 在请求头**注入 `X-API-Version-Id`**（网关侧做 path → version 的解析）。
> - **dispatcher** 是**纯转发**：不做 path 解析、不维护路由表，仅依据 APISIX 已注入的 `X-API-Version-Id` 取 upstream / 元数据后转发。
> - **api-registry** 在 `publish` 时通过 **APISIX Admin API** 下发路由（upstream 指向 `DISPATCHER_UPSTREAM`）；`retire` 不摘除路由，dispatcher 按 `status='retired'` 返 `410 Gone`。
>
> 这条边界排除了「dispatcher 自己解析 path 选 version」和「api-registry 手动静态写路由」两类过时设计。

## R1d：APISIX consumer / 路由 / 可信入口归属

- **APISIX consumer** 由 **auth** 管（随 APIKey 生命周期）：`create_key` 建 consumer
  （username=`key_id`，per-key）+ 预热 Redis 身份缓存；`revoke_key` 删 consumer + 清缓存。
  auth 是 app/key 聚合的唯一拥有者（§9-B），故 consumer 归它。
- **APISIX route** 由 **api-registry** 管（随 publish）：`publish_route` 下发（R1c），
  带 key-auth + 条件 limit-count + 注入 X-API-Version-Id / X-Ingress-Auth。
- **可信入口不变量（安全）**：dispatcher 的 `authenticate_request` 信任 `X-Ingress-Auth`
  header 跳过 HTTP auth 回源（修 good-key 503）。此信任成立的前提是 **dispatcher 仅经
  APISIX 可达（ClusterIP，无外部 ingress）**——APISIX proxy-rewrite 用 `set` 覆写调用方
  提供的同名 header，故只有经 APISIX 的请求才带可信值。若 dispatcher 被外部直连暴露，
  该 header 可伪造、绕过鉴权。任何新增 dispatcher 暴露面（额外 ingress/NodePort）前必须
  重审此不变量。
