# 11 · 多租户设计

> 本文档落实 [ADR-009](00-decisions.md#adr-009-多租户策略) 的"平台多租户"决策。

## 1. 租户模型

### 1.1 租户定义

**租户（Tenant）= 数据隔离 + 配额独立 + 报表独立的逻辑单元**。

| 租户类型 | 例子 | 默认配额 |
|---------|------|---------|
| `internal` | 公司各业务线（用户服务、订单、支付等）/ 子公司 | 高（按业务线谈） |
| `external` | 外部接入公司 | 低（按套餐） |
| `system` | 平台自身（监控、健康检查、内部 API） | 无限 |

### 1.2 租户层级

支持父子租户，便于集团 → 子公司 → 业务线的层级：

```
集团（root_tenant）
├── 子公司 A（internal_tenant_a）
│   ├── 业务线 A1
│   └── 业务线 A2
├── 子公司 B
└── 外部公司 X（external_tenant_x）
```

字段：`tenant.parent_id` 指向父租户。查询时支持"含子租户"模式（管理后台用）。

### 1.3 默认租户

平台初始化时创建：

| tenant_id | code | type | 用途 |
|-----------|------|------|------|
| 1 | `system` | system | 平台内部 API（健康检查、metrics） |
| 2 | `internal-default` | internal | 默认内部业务线（未指定租户时归此） |
| 3 | `external-public` | external | 普通外部开发者（个人开发者，不归属某公司） |

外部公司接入时新建 tenant，归属 `external` 类型。

## 2. 隔离方式

### 2.1 双重隔离

| 层 | 实现 | 用途 |
|----|------|------|
| 应用层 | 所有查询强制带 `WHERE tenant_id = ?` | 主隔离 |
| 数据库层 | PostgreSQL Row Level Security（RLS） | 兜底防漏 |

### 2.2 应用层隔离

#### 强制约束
- 所有 ORM 模型继承 `TenantMixin`，自动带 `tenant_id` 字段
- 所有 query 强制 `filter(tenant_id=current_tenant.id)`
- FastAPI Dependency 注入 `current_tenant`，从 JWT / API Key 解析
- 单元测试用 lint 规则检查：`grep "SELECT.*FROM" | grep -v "tenant_id"` 报警

#### Pydantic 模型示例

```python
class TenantScopedModel(BaseModel):
    tenant_id: int

class APIRegistry:
    async def get_api(self, api_id: int, tenant: Tenant) -> API:
        # 强制带 tenant_id
        return await db.fetch_one(
            "SELECT * FROM api_definition WHERE id = $1 AND tenant_id = $2",
            api_id, tenant.id
        )
```

### 2.3 PostgreSQL RLS 兜底

```sql
-- 启用 RLS
ALTER TABLE api_definition ENABLE ROW LEVEL SECURITY;

-- 策略：当前会话变量 app.current_tenant_id 决定可见行
CREATE POLICY tenant_isolation ON api_definition
    USING (tenant_id = current_setting('app.current_tenant_id')::bigint);

-- 应用每次连接设 session 变量
-- SET app.current_tenant_id = '123';
```

应用层在连接池获取连接后立即 `SET LOCAL app.current_tenant_id = ?`，确保即使 SQL 漏写 `WHERE tenant_id`，也不会跨租户读数据。

### 2.4 不选 Schema 隔离 / DB 隔离的原因

| 方案 | 劣势 |
|------|------|
| Schema 隔离（每租户独立 schema） | 100+ 租户 = 100+ schema，DDL 变更困难，连接池管理复杂 |
| DB 隔离（每租户独立 DB） | 成本爆炸，运维灾难 |

应用层 + RLS 兼顾性能、隔离、运维成本。

## 3. 租户上下文传播

### 3.1 入口解析

```
请求进入
   │
   ├─ APISIX 提取 API Key
   │
   ├─ auth 服务校验 Key
   │     │
   │     └─ 查 app_api_key → app → tenant
   │
   ├─ 注入 HTTP Header:
   │     X-Tenant-Id: 123
   │
   └─ 下游服务透传
```

下游服务读取 `X-Tenant-Id`，写入 trace、log、Kafka 消息、PG 连接 session 变量。

### 3.2 跨服务传播

| 载体 | 字段 |
|------|------|
| HTTP Header | `X-Tenant-Id` |
| gRPC metadata | `x-tenant-id` |
| Kafka message header | `tenant_id` |
| DB session | `app.current_tenant_id` |
| Redis Key 前缀 | `t:{tenant_id}:...` |
| 日志字段 | `tenant_id` |
| Trace tag | `tenant.id` |

### 3.3 上下文丢失告警

业务服务若收到的请求无 `X-Tenant-Id`（除公开 API 外）：
- 立即拒绝（403）
- 触发告警（可能 RLS 配置错误或网关配置错误）

## 4. 数据模型影响

### 4.1 加 tenant_id 的表

所有元数据 / 业务表都加（部分 system 表除外）：

| 表 | tenant_id 含义 |
|----|---------------|
| `api_definition` | 接口归属租户 |
| `api_version` | 同上（继承 api） |
| `app` | 应用归属租户 |
| `app_api_key` | 同上（继承 app） |
| `app_api_grant` | 授权关系（调用方租户） |
| `task_instance` | 异步任务归属租户 |
| `retry_task` | 重试任务归属租户 |
| `audit_log` | 操作归属租户 |
| `api_change_request` | 变更工单归属租户 |
| `user` | 用户归属租户 |
| `notification` | 通知归属租户 |

### 4.2 不加 tenant_id 的表

| 表 | 原因 |
|----|------|
| `tenant` 自身 | 元数据 |
| `tenant_member` | 用户与租户的多对多 |
| `system_dict` | 系统字典 |
| `region_info` | 区域信息 |

### 4.3 调用日志（ClickHouse）

```sql
CREATE TABLE api_call_log (
    -- 新增
    tenant_id       UInt64,
    tenant_code     LowCardinality(String),
    -- 其他字段同前...
    -- ORDER BY 加入 tenant_id，便于按租户聚合
) ENGINE = MergeTree
PARTITION BY toYYYYMMDD(ts)
ORDER BY (ts_hour, tenant_id, api_id, app_id, trace_id);
```

### 4.4 物化视图按租户

```sql
CREATE MATERIALIZED VIEW api_call_stats_by_tenant
ENGINE = SummingMergeTree()
PARTITION BY toYYYYMMDD(ts_hour)
ORDER BY (ts_hour, tenant_id, api_id, app_id, env, http_status)
AS
SELECT
    ts_hour, tenant_id, api_id, app_id, env, http_status,
    count() AS call_count,
    sum(latency_ms) AS total_latency_ms,
    ...
FROM api_call_log
GROUP BY ts_hour, tenant_id, api_id, app_id, env, http_status;
```

## 5. 配额按租户

### 5.1 三层配额

| 层 | 维度 | 含义 |
|---|------|------|
| 租户级 | `tenant_quota` | 整个租户的总配额 |
| 应用级 | `app_quota` | 单个应用配额（继承或自定义） |
| API 级 | `api_quota` | 单个接口的限流 |

三者合并取最严。

### 5.2 Redis Key 设计

```
t:{tenant_id}:rate:{api_id}:{app_id}:{minute_slot}    # 应用 × API 维度
t:{tenant_id}:rate:app:{app_id}:{minute_slot}          # 应用总维度
t:{tenant_id}:rate:tenant:{minute_slot}                # 租户总维度
```

### 5.3 配额预扣

为避免租户级配额超用，每次调用前先检查三层：

```python
async def check_quota(tenant_id, app_id, api_id):
    keys = [
        f"t:{tenant_id}:rate:{api_id}:{app_id}:{minute_slot}",
        f"t:{tenant_id}:rate:app:{app_id}:{minute_slot}",
        f"t:{tenant_id}:rate:tenant:{minute_slot}",
    ]
    limits = [
        api.rate_limit.qps,
        app.rate_limit.qps,
        tenant.rate_limit.qps,
    ]
    # Lua 脚本原子检查 + 增加
    allowed = await redis.eval(QUOTA_LUA, keys, limits)
    if not allowed:
        raise RateLimitError()
```

## 6. 跨租户场景

### 6.1 默认禁止

- 调用日志：仅能查本租户的
- 应用列表：仅能看本租户的
- 配置变更：仅能改本租户的接口

### 6.2 平台运维跨租户

`super_admin` 角色可跨租户：
- 后台 UI 切换租户（顶部下拉）
- 调用 API 时显式传 `X-Tenant-Override`（仅超管可用）
- 所有跨租户操作 → 审计 + 告警

### 6.3 父租户聚合

子公司租户的管理员可看子租户数据：
- 查询时 `WHERE tenant_id IN (self_id, child_ids)`
- 仅限于查看（变更仍按租户独立）

### 6.4 跨租户调用

业务上一般不允许（不同子公司之间互相调用 API）。如需允许：
- 接口提供方在 `visibility` 设为 `cross_tenant`
- 授权关系 `app_api_grant` 跨租户
- 调用日志按调用方租户归属

## 7. 租户生命周期

### 7.1 创建

- 平台运维 / 超管创建租户
- 字段：`code` / `name` / `type` / `parent_id` / `quota_config` / `status`

### 7.2 接入

- 租户管理员创建 `user` 账号
- 创建 `app`
- 生成 API Key
- 申请 API 授权

### 7.3 配额调整

- 租户级配额由超管调整
- 应用级配额由租户管理员调整
- API 级限流由接口提供方调整

### 7.4 暂停 / 退出

| 状态 | 行为 |
|------|------|
| active | 正常 |
| suspended | 该租户所有调用拒绝，管理员可登录查看 |
| closed | 所有 Key 失效，数据保留 6 个月后归档 |

## 8. 租户管理服务（tenant-svc）

### 8.1 职责

- 租户 CRUD
- 租户成员管理（用户与租户的多对多）
- 配额规则管理
- 租户状态变更

### 8.2 主要 API

```
POST   /admin/tenants                 创建租户（超管）
GET    /admin/tenants                 列表（按权限过滤）
GET    /admin/tenants/{id}            详情
PUT    /admin/tenants/{id}            更新
POST   /admin/tenants/{id}/suspend    暂停
POST   /admin/tenants/{id}/resume     恢复
POST   /admin/tenants/{id}/close      关闭

POST   /admin/tenants/{id}/members    添加成员
DELETE /admin/tenants/{id}/members/{user_id}
PUT    /admin/tenants/{id}/members/{user_id}  改角色

GET    /admin/tenants/{id}/quota      查配额
PUT    /admin/tenants/{id}/quota      改配额
GET    /admin/tenants/{id}/usage      查用量
```

### 8.3 缓存策略

租户元数据极热（每次调用都查）：
- Redis 全量缓存
- Key: `t:{tenant_id}:meta`
- TTL 30min，更新时主动失效

## 9. 安全与审计

### 9.1 强制审计

| 操作 | 必审 |
|------|------|
| 创建 / 修改 / 删除租户 | ✓ |
| 添加 / 删除租户成员 | ✓ |
| 调整租户配额 | ✓ |
| 暂停 / 关闭租户 | ✓ |
| 跨租户访问（超管） | ✓ |

### 9.2 数据脱敏

跨租户展示时（如平台运维视图），租户名称部分隐藏：
- `某互联网公司` → `某互联网****`
- 仅超管可见完整名称

### 9.3 数据导出限制

- 单次导出限本租户
- 超管跨租户导出需审批 + 二次确认 + 审计

## 10. 多租户下的性能考虑

### 10.1 索引

所有表索引首位加 `tenant_id`：

```sql
CREATE INDEX idx_api_def ON api_definition(tenant_id, status);
CREATE INDEX idx_app ON app(tenant_id, owner);
```

避免大表全表扫跨租户。

### 10.2 ClickHouse 分区

调用日志 ORDER BY 加 `tenant_id`：
- 单租户查询走 partition pruning + 索引
- 大租户查询不会卡小租户

### 10.3 缓存隔离

Redis Key 加 `t:{tenant_id}:` 前缀，避免 Key 命名冲突 + 便于按租户清理。

### 10.4 Kafka 分区

调用事件分区键：`{tenant_id}:{trace_id}` hash → 同租户的事件顺序集中。

## 11. 与 Portal / Admin 的关系

### 11.1 Admin 后台

- 顶部下拉切换"当前租户"
- 默认显示用户所属租户
- 超管可切换任意租户
- 切换操作审计

### 11.2 Portal 门户

- 外部开发者注册时创建 / 加入租户
- 个人开发者 → 加入 `external-public` 租户
- 企业开发者 → 创建新的 `external` 租户（需企业认证）
- 一个邮箱可关联多个租户（员工跳槽场景）

## 12. 多租户迁移路径

如业务发展需要升级到"商业多租户"（卖给外部企业作为独立产品）：

1. `tenant.type` 增加 `customer` 类型
2. 计费独立（billing 按租户结算）
3. 白标 / 自定义品牌
4. 数据库实例级别隔离（每大客户独立 RDS）

详见未来 ADR。

## 13. 测试策略

### 13.1 单元测试

- 每个 service 测试覆盖"租户隔离生效"
- 测试用例：租户 A 创建数据，租户 B 查询，必须返回空

### 13.2 集成测试

- 自动化测试：跨租户访问必须 403
- RLS 测试：临时去掉应用层 WHERE，RLS 仍生效

### 13.3 渗透测试

- 上线前必须做跨租户越权测试
- 每季度回归
