# dispatcher

> 统一调用入口 —— 同步 HTTP 转发 + 异步任务派发 + AI 流式代理。
> 详见 [docs/03-services.md §3.4](../../../docs/03-services.md) + [docs/05-core-flows.md §2](../../../docs/05-core-flows.md)。

## 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| ANY  | `/dispatch/{rest}` | catch-all 调用入口（APISIX 转发） |
| GET  | `/v1/dispatcher/health` | 自身健康检查 |
| GET  | `/health/live` / `/health/ready` | k8s probe |
| GET  | `/metrics` | Prometheus |

## 路由解析

```
incoming request
   ↓
有 X-API-Version-Id?  → 直接按 ID 查 + Redis 缓存（5min）
   ↓ 否
按 path + method 在已发布接口中找（dev 直连回退）
   ↓
按 backend_type 分流：
  http        → HttpForwarder.forward 同步转发
  ai_model    → 流式（SSE）或同步
  async_task  → task_dispatcher：写 PG + 推 Kafka + 返回 202 task_id
  workflow    → Phase 2
```

## 关键设计

### 1. httpx.AsyncClient 单例

启动时创建一个全局 `AsyncClient(max_connections=500, http2=True)`，所有请求复用 TCP 连接，避免每次 SSL 握手开销。

### 2. AI SSE 流式

`backend_type=ai_model + ai_streaming=true` 时返回 `StreamingResponse`，chunk-by-chunk 转发，每个 chunk 解析 token usage（OpenAI 格式），结束时一次 emit。

### 3. 异步任务 at-least-once

```
1. 写 PG task 表（pending）—— 先持久化
2. 推 Kafka task-requests —— 后发事件
3. 返回 202 + task_id
```

如果 Kafka 发送失败，task 表仍是 pending，executor 拉取不到但 dispatcher 也能后续重试。

### 4. 调用事件异步投递

不阻塞响应链路。`/dispatch/*` 完成后 fire-and-forget Kafka emit。

## 本地开发

```bash
make install
make dev-up              # 起 PG/Redis/Kafka
make run-dispatcher      # uvicorn dispatcher.main:app --reload --port 8001
```

调用示例：

```bash
# 假设 api-registry 已发布 ver_demo_a_v1（HTTP）
curl -H "X-API-Key: ak_test_a_demo001" \
     -H "X-API-Version-Id: ver_demo_a_v1" \
     http://localhost:8001/dispatch/v1/users/u_001
```

## 测试

```bash
cd services/services/dispatcher
pytest tests/ -v
# 44 tests, all pass
```

覆盖：
- masking（11）—— 各种 action / 嵌套字段 / 数组
- event payload 构造（9）—— 字段齐全 + 类型转换
- path 匹配（24）—— resolver 路由解析

## 性能预算（prod）

- 单副本 4000m CPU / 4Gi 内存
- 8 副本起步（HPA 自动扩到 20+）
- 单实例 keepalive 100 连接，最大 500 连接
- 同步 HTTP：P99 < 50ms 转发开销
- AI 流式：first chunk < 200ms
