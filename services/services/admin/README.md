# admin-bff

> 后台管理 BFF —— 聚合审计 + 跨服务概览。
> 详见 [docs/03-services.md §3.2](../../../docs/03-services.md) + [docs/08-audit.md](../../../docs/08-audit.md)。

把 admin UI 需要的所有后台接口聚到一个入口：
1. 审计查询（list/detail/stats/export）+ 手动写入（内部服务）
2. 自动审计 middleware —— 所有 mutation 自动落 `audit_log`
3. Dashboard —— 跨服务聚合（tenant-svc 调一次列全部租户，本地算 active/suspended/closed）

## 架构

```
admin UI / 内部服务
        ↓
        /v1/admin/*  (admin-bff)
        ↓
   ┌────┴─────┐
   ↓          ↓
audit_log   AggregatorClient (httpx) → tenant-svc / auth-svc
(PG)
```

## 自动审计

FastAPI middleware 在每个 mutation（POST/PUT/PATCH/DELETE）请求结束时调 `record_from_request`：

```python
if (request.method.upper() in ("POST", "PUT", "PATCH", "DELETE")
    and request.url.path.startswith("/v1/admin/")
    and not request.url.path.startswith("/v1/admin/health")):
    await record_from_request(request, status_code=response.status_code)
```

action 推断从 path 走（不依赖开发者手写 `action=xxx`）：
1. 扫描路径段，记录**最深**的已知资源关键词（`tenants`/`apis`/`api-keys`/...）
2. 资源后的第一个段 = `resource_id`
3. 末段如果是已知 verb（`suspend`/`publish`/`reset`/...）→ action = `{verb}_{resource}`
4. 否则按 HTTP 方法：POST→create / PUT,PATCH→update / DELETE→delete

例如：
- `POST /v1/admin/tenants` → `create_tenants`
- `DELETE /v1/admin/tenants/t1/members/u2` → `delete_members`（最深资源赢）
- `POST /v1/admin/tenants/t1/suspend` → `suspend_tenants`

新加资源时记得在 `_KNOWN_RESOURCES` / `_KNOWN_VERBS` 登记。

## 为什么写审计用 admin_db_session

`audit_log` 表本身就有 `tenant_id` 列，理论上 RLS 可以挂。但审计写入用 `db_session`（带 RLS）有死锁风险：业务 transaction 内再起一个 audit transaction，如果 audit 失败会让业务回滚 —— **审计挂了不应该影响业务**。

所以策略是：
- **写**：`admin_db_session`（绕 RLS）+ best-effort（失败 log warning 返回 0，不抛）
- **读**：`db_session`（普通用户视角强制 `tenant_id = viewer_tenant_id`）或 `admin_db_session`（超管视角）

## 接口

| 方法 | 路径 | 鉴权 | 说明 |
|------|------|------|------|
| GET  | `/v1/admin/audit` | 超管（全部）/ 普通用户（自己租户） | 列表查询（支持 tenant_id/actor/action/resource/since/until/limit/offset） |
| GET  | `/v1/admin/audit/stats` | 同上 | top_actions/top_actors/by_day（days 1-90，默认 7） |
| GET  | `/v1/admin/audit/{id}` | 同上 | 单条详情（含 IP/UA/detail） |
| GET  | `/v1/admin/audit/export/csv` | 超管 | Phase 2（目前返 501） |
| POST | `/v1/admin/audit/record` | 内部服务 | 手动写一条（不强制 admin，NetworkPolicy 兜底） |
| POST | `/v1/admin/audit/record-batch` | 内部服务 | 批量写 |
| GET  | `/v1/admin/dashboard` | **超管 only** | 跨服务聚合（tenants 状态分布 + 审计今日/7d + 最近 10 条） |
| GET  | `/v1/admin/health` | 无 | k8s probe |

## AggregatorClient

```python
agg = get_aggregator()           # singleton httpx.AsyncClient
tenants = await agg.list_tenants(api_key=api_key)
```

- 超时 3s（dashboard 慢一点不要紧，但不能挂）
- 最大并发 50 连接
- 任何下游失败 → 返回 `[]` 或 `None`（graceful degradation，dashboard 部分数据缺失优于全挂）
- 路径走 K8s 内部 DNS：`http://tenant.apihub-system.svc.cluster.local`

## 等保三级要点（docs/14 §5）

- 审计写入失败**必须**告警（best-effort 是写入路径，不是丢失路径 —— 失败时打 warning log + 监控告警）
- 审计详情含 IP / UA / trace_id —— 满足追溯要求
- 普通用户访问 dashboard → 403（聚合数据跨租户）
- 普通用户看其他租户的审计 → `_build_where` 强制覆盖 `tenant_id`

## 本地开发

```bash
make install
make dev-up              # 起 PG/Redis/Kafka
make run-admin           # uvicorn admin.main:app --port 8006

# 顺便起 tenant-svc 让 dashboard 能聚合
make run-tenant
```

手动测一下：
```bash
# 列审计（需要超管）
curl -s localhost:8006/v1/admin/audit -H 'X-API-Key: <admin-key>' | jq

# 内部服务写一条
curl -s localhost:8006/v1/admin/audit/record \
  -H 'Content-Type: application/json' \
  -d '{"tenant_id":"t1","action":"custom_event","resource_type":"custom"}' | jq

# Dashboard（仅超管）
curl -s localhost:8006/v1/admin/dashboard -H 'X-API-Key: <admin-key>' | jq
```

## 测试

```bash
cd services/services/admin
pytest tests/ -v
# 44 tests, all pass
```

覆盖：
- `test_repository.py`（17）—— record/record_many（含部分失败计数）+ _build_where（viewer 强制 / admin 跨租户 / 全过滤）+ list/get/count/stats
- `test_audit.py`（9）—— `_infer_action` 各种 path（基础资源 / 嵌套 / verb / api-keys 中划线 / GET 跳过 / health 跳过）
- `test_routes.py`（18）—— 端点权限矩阵（admin vs 普通用户）+ query 参数解析 + dashboard 聚合 + middleware 自动审计（mutation 触发 / GET 不触发）

mock 策略：
- DB：`_FakeConn` 记录所有 SQL/params，断言被调用的 pattern（不连真 PG）
- Aggregator：`monkeypatch.setattr(get_aggregator().__class__, "list_tenants", _fake)`（实例方法替换需要 `self`）
- 鉴权：`as_platform_admin` / `as_normal_user` fixture 切换 TenantContext

## 性能预算（prod）

- 5 副本（docs/03 §3.14，管理类）
- 单副本 1 CPU / 1Gi
- HPA 基于 CPU 70%
- AggregatorClient 全局共享一个连接池（避免每个请求新建 client 的开销）

## 关联

- 上游：admin UI（Vue）/ portal-bff（部分复用审计查询）
- 下游：tenant-svc（dashboard 聚合）、auth-svc（未来扩展）
- 数据：`audit_log` 表（PG，按 `created_at` 分月管理，Phase 2 实现 OSS 归档）
