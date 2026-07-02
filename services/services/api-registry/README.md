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
