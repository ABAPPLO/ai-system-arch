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
