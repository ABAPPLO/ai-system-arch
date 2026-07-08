# api-registry

> 接口元数据管理服务。详见 [docs/03-services.md §3.1](../../../docs/03-services.md)。

## 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/v1/apis` | 创建接口 |
| GET | `/v1/apis` | 接口列表（RLS 自动过滤本租户） |
| GET | `/v1/apis/{api_id}` | 接口详情 |
| POST | `/v1/api-versions` | 创建新版本 |
| POST | `/v1/api-versions/{version_id}/publish` | 发布版本 |
| POST | `/v1/api-versions/{version_id}/deprecate` | 标记废弃（published → deprecated） |
| POST | `/v1/api-versions/{version_id}/retire` | 下线（deprecated → retired，APISIX 摘路） |
| POST | `/v1/change-requests` | 提交变更工单（dev 自助 / staging·prod 走审批） |
| GET | `/v1/change-requests` | 工单列表（支持 api_id/status/change_type/target_env 过滤） |
| GET | `/v1/change-requests/{id}` | 工单详情 |
| POST | `/v1/change-requests/{id}/approve` | 审批通过（仅 platform_admin） |
| POST | `/v1/change-requests/{id}/reject` | 退回（仅 platform_admin） |
| POST | `/v1/change-requests/{id}/cancel` | 提交方撤回（仅 pending） |
| POST | `/v1/change-requests/{id}/apply` | 执行 approved 工单的副作用（发布/下线） |
| GET | `/v1/change-requests/health` | 治理子系统健康检查 |
| GET | `/health/live` | 存活检查 |
| GET | `/health/ready` | 就绪检查 |
| GET | `/metrics` | Prometheus 指标 |

## 本地开发

```bash
# 1. 安装 apihub-core（编辑模式）
cd services/libs/apihub-core && pip install -e . && cd -

# 2. 安装本服务
cd services/services/api-registry && pip install -e . && cd -

# 3. 配置环境变量
export PG_HOST=localhost
export PG_USER=apihub
export PG_PASSWORD=xxx
export REDIS_HOST=localhost
export KAFKA_BROKERS=localhost:9092
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
export LOG_LEVEL=DEBUG

# 4. 启动
uvicorn api_registry.main:app --reload --port 8000
```

## 构建镜像

需要在仓库根目录执行（构建上下文要包含 libs）：

```bash
docker build -f services/services/api-registry/Dockerfile \
  -t registry.apihub.internal/apihub/api-registry:0.1.0-dev .
```

## 关键模式

### RLS 自动租户隔离

所有 SQL 都不写 `WHERE tenant_id = ?`，由 `db_session()` 在事务开头 `SET LOCAL app.tenant_id` 后 RLS 自动过滤。

```python
async with db_session() as conn:
    # 即使业务忘了加 tenant_id 条件，RLS 也保证只看到本租户数据
    rows = await conn.fetch("SELECT * FROM api")
```

### 事件审计

所有变更操作通过 Kafka `audit-events` 异步审计：

```python
await kafka.emit("audit-events", {
    "action": "api.create",
    "resource_type": "api",
    "resource_id": api_id,
    "detail": payload.model_dump(),
})
```

### 接口生命周期（draft → published → deprecated → retired）

```
draft ──publish──→ published ──deprecate──→ deprecated ──retire──→ retired
                      ↑                                          (410 Gone)
                      └── 直接从 draft 发版（Phase 1 简化）
```

- **published**：APISIX 路由生效，调用方正常使用。
- **deprecated**：仍可调用，给调用方迁移时间（响应头 `Sunset`）。
- **retired**：APISIX 摘路，所有调用收到 `410 Gone`。
- **安全闸**：`retire` 必须先 `deprecate`，不允许 `published → retired` 直跳，避免误下线。

### 变更评审工单（ADR-005 分级审批）

按目标环境分级（`target_env`）：

| env | 流程 | 自动 apply |
|-----|------|-----------|
| `dev` | 提交即通过（自助） | ✅ submit 时自动执行 |
| `staging` | 钉钉审批单 + 平台 review | 审批通过后调 `/apply` |
| `prod` | 钉钉强审批 + 平台运维 review | 审批通过后调 `/apply` |

```
pending ──approve──→ applied（执行副作用：发布 / 下线）
   │                    ↑
   ├──rejected──→ rejected
   │
   └──cancelled（提交方撤回）
```

- **审批权限**：`approve` / `reject` 仅 `platform_admin`（403 否则）。
- **撤回权限**：`cancel` 仅原提交方 + 状态为 `pending`。
- **幂等**：`/apply` 对非 `approved` 工单返回 409。
- **钉钉回调**：钉钉审批通过 → webhook → 自动 approve + apply（Phase 3 接入）。

#### 典型流程

```bash
# 1. 业务方提交 prod 发布工单
POST /v1/change-requests
{ "api_id": 100, "target_version": "v2",
  "change_type": "publish", "target_env": "prod",
  "proposed_config": { ... }, "submitted_by": "u_alice" }
→ 201 { "request_id": 1, "status": "pending" }

# 2. 平台运维审批
POST /v1/change-requests/1/approve
{ "review_comment": "lgtm" }
→ 200 { "status": "approved" }

# 3. 触发实际发布（update api_version.status='published' + 下发 APISIX）
POST /v1/change-requests/1/apply
→ 200 { "status": "applied", "summary": "..." }
```
