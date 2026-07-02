# apihub-core

> APIHub 微服务共享基础库 —— 所有 Python 服务统一接入。

## 提供什么

| 模块 | 解决什么 |
|------|---------|
| `config` | 环境变量集中读取（pydantic-settings） |
| `tenant` | 租户上下文 contextvar（HTTP→Kafka→DB→日志全链路贯穿） |
| `db` | asyncpg 连接池 + RLS 自动注入 `SET LOCAL app.tenant_id` |
| `redis` | 租户前缀客户端（key 自动加 `t:{tenant_id}:` 前缀） |
| `kafka` | 事件投递自动注入 tenant_id header + 用 tenant_id 做分区 key |
| `tracing` | OTel SDK 初始化 + FastAPI/httpx/asyncpg/redis 自动 instrumentation |
| `logging` | structlog + 自动关联 OTel trace_id/span_id |
| `auth` | APIKey 校验 + 回填 TenantContext |
| `errors` | 统一错误码 + 统一 JSON 错误响应 |
| `middleware` | FastAPI 应用工厂（一次接入全套） |

## 用法

### 1. 安装

```bash
cd services/libs/apihub-core
pip install -e .
```

### 2. 业务服务接入

```python
# services/api-registry/src/api_registry/main.py
from fastapi import FastAPI
from apihub_core import create_app, db, redis, kafka, TenantContext

def register_routes(app: FastAPI) -> None:
    @app.get("/v1/apis/{api_id}")
    async def get_api(api_id: str):
        async with db_session() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM api WHERE id = $1", api_id
            )
            # 不用写 WHERE tenant_id = ?，RLS 自动过滤
            ...
        await emit("audit-events", {"action": "api.view", "api_id": api_id})

app = create_app(
    service_name="api-registry",
    build_routes=register_routes,
)
```

### 3. 启动

```bash
uvicorn api_registry.main:app --host 0.0.0.0 --port 8000 --workers 4
```

## 关键约束

1. **不要绕过 `db_session` / `t_get` / `emit`** —— 这是租户隔离的根基。
2. **不要硬编码 tenant_id** —— 一律从 `require_tenant()` 取。
3. **平台运维跨租户操作** —— 用 `raw_client()` 并显式审计。
