# tenant-svc

> 租户管理服务 —— 落实 [ADR-009 平台多租户](../../../docs/00-decisions.md)。
> 详见 [docs/03-services.md §3.14](../../../docs/03-services.md) + [docs/11-multi-tenant.md](../../../docs/11-multi-tenant.md)。

## 架构

```
client (admin) → POST /v1/tenant/tenants
                       ↓
                  鉴权 + 权限检查（超管？租户成员？）
                       ↓
                  repository (admin_db_session)
                       ↓
                  PG tenant 表 + tenant_member 表
                       ↓
                  cache.set / cache.invalidate (Redis)
                       ↓
                  上游服务（auth/quota/dispatcher）下次请求读到新状态
```

## 为什么 tenant 表用 admin_db_session

`tenant` 表没有 `tenant_id` 列（它本身就是租户元数据）—— RLS 没法挂。隔离由应用层保证：

| 操作 | 权限 |
|------|------|
| 创建租户 / 列全部 / suspend / resume / close | **仅超管** |
| 改 tenant name/slug/tier | 仅超管 |
| 改配额（PUT /quota） | 仅超管 |
| 加/删/改成员 | 超管 OR 该租户 owner/admin |
| GET 租户详情 / 成员列表 / 配额 / 用量 | 超管 OR 该租户任意 role |

权限检查在 router 内做（`_require_platform_admin` / `_require_tenant_role`），不依赖 RLS。

## 状态机

```
active ←──── resume ────┐
   │                    │
   │ suspend            │
   ↓                    │
suspended ──resume──────┘
   │
   │ close
   ↓
closed （终态，不可恢复）
```

非法转换 → `409 CONFLICT`。已 closed → 任何转换都抛 CONFLICT。

```python
# repository.change_status 的 SQL 也带 WHERE status = ANY($3)
# 防止应用层校验后、DB UPDATE 前被并发改了
UPDATE tenant SET status = $1
WHERE id = $2 AND status = ANY($3::text[])
```

## Redis 缓存策略

key: `t:{tenant_id}:meta`，TTL 30min（docs/11 §8.3）。

| 操作 | 缓存动作 |
|------|---------|
| create / update / resume | `cache.set`（warmup，让上游立刻读到） |
| suspend / close / 改配额 | `cache.invalidate`（强制上游回 PG） |

为什么 suspend 要失效而不是写 "suspended" 状态？让 auth 服务下次 verify APIKey 时回 PG 查，发现 status≠active 直接拒。如果缓存里写 "suspended" 也行，但失效更简单 + 状态变更更明确（"下次访问必须重读"）。

为什么 close 失效而不删除整个 key？key 不存在 → 缓存 miss → 回 PG。和失效等价，但少一次 SET。

## 接口

| 方法 | 路径 | 鉴权 | 说明 |
|------|------|------|------|
| POST | `/v1/tenant/tenants` | 超管 | 创建租户 |
| GET  | `/v1/tenant/tenants` | 超管（全部）/ 普通用户（自己加入的） | 列表 |
| GET  | `/v1/tenant/tenants/me` | 任意 | 当前用户加入的所有租户（前端切换用） |
| GET  | `/v1/tenant/tenants/{id}` | 超管 OR 成员 | 详情（普通用户 name 脱敏） |
| PUT  | `/v1/tenant/tenants/{id}` | 超管 | 更新 name/slug/tier/metadata |
| POST | `/v1/tenant/tenants/{id}/suspend` | 超管 | 暂停 |
| POST | `/v1/tenant/tenants/{id}/resume` | 超管 | 恢复 |
| POST | `/v1/tenant/tenants/{id}/close` | 超管 | 关闭（终态） |
| GET  | `/v1/tenant/tenants/{id}/members` | 超管 OR 成员 | 成员列表 |
| POST | `/v1/tenant/tenants/{id}/members` | 超管 OR owner/admin | 加成员 |
| PUT  | `/v1/tenant/tenants/{id}/members/{user_id}` | 超管 OR owner/admin | 改成员角色 |
| DELETE | `/v1/tenant/tenants/{id}/members/{user_id}` | 超管 OR owner/admin | 删成员 |
| GET  | `/v1/tenant/tenants/{id}/quota` | 超管 OR 成员 | 查配额 |
| PUT  | `/v1/tenant/tenants/{id}/quota` | 超管 | 改配额（触发缓存失效） |
| GET  | `/v1/tenant/tenants/{id}/usage` | 超管 OR 成员 | 当日用量（Phase 3 接 quota/analyzer） |
| GET  | `/v1/tenant/tenants/{id}/children` | 超管 OR 成员 | 子租户列表 |
| GET  | `/v1/tenant/health` | 无 | k8s probe |

## 角色

`tenant_member.role` 取值：

| 角色 | 能做 |
|------|------|
| `owner` | 全部 + 改成员 + 转让 owner |
| `admin` | 加/删/改成员（除 owner） + 改接口 |
| `developer` | 创建/编辑接口、看用量 |
| `viewer` | 只读 |

角色权限不在 tenant-svc 内强制（除成员管理），由下游服务（api-registry/dispatcher 等）的 `required_scopes` 检查。

## 脱敏

普通用户看其他租户的 name 时，前端会拿不到完整值：
- `"某互联网公司"` → `"互********"` （首字保留，其余 `*`）
- 长度 ≤ 2 的名字不脱敏（避免全星号）
- 仅超管看完整 name

## 本地开发

```bash
make install
make dev-up              # 起 PG/Redis/Kafka
make run-tenant          # uvicorn tenant.main:app --port 8005
```

手动测一下（需要超管 API Key）：
```bash
# 列出自己的租户
curl -s localhost:8005/v1/tenant/tenants/me -H 'X-API-Key: ak_test_a_demo001' | jq

# 超管创建租户
curl -s localhost:8005/v1/tenant/tenants \
  -H 'X-API-Key: <admin-key>' \
  -H 'Content-Type: application/json' \
  -d '{"id":"t_new","name":"新租户","slug":"new","type":"internal"}' | jq
```

## 测试

```bash
cd services/services/tenant
pytest tests/ -v
# 41 tests, all pass
```

覆盖：
- `test_repository.py`（9）—— 状态机预检（bad status / closed 终态 / active→suspended 调 DB）+ 角色合法性 + 空更新短路 + tier 归一化
- `test_cache.py`（7）—— set/get + missing + invalidate + 批量失效 + 损坏 JSON 自动删 + TTL 30min 验证
- `test_routes.py`（25）—— CRUD + 状态机端点 + 成员管理 + 配额 + 用量 + 子租户 + 名字脱敏 + 权限矩阵

mock 策略：
- DB 层：每个 test 自己 `monkeypatch` repository 的具体函数（不实现完整 PG mock）
- Redis 层：fakeredis（真跑 SETEX / DEL / json）
- 鉴权：`authed` / `as_platform_admin` / `as_normal_user` fixture 切换 TenantContext

## 性能预算（prod）

- 5 副本（docs/03 §3.14）
- 单副本 1 CPU / 1Gi（管理类 QPS 不高，但批量操作可能重）
- HPA 基于 CPU 70%

## 关联

- 上游：admin-bff 调用做后台 UI；portal-bff 调用让用户加入/切换租户
- 下游：auth 读 `t:{tenant_id}:meta` 快速校验租户状态；quota 读 `metadata.quota` 算三层规则
- 数据：`tenant` 表无 RLS（应用层管理），`tenant_member` 表有 RLS（用户只能查自己加入的）
