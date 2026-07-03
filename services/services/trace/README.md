# trace-svc

> 调用追踪服务 —— ClickHouse 调用日志查询 + 聚合分析。
> 详见 [docs/03-services.md §3.8](../../../docs/03-services.md)。

## 架构

```
dispatcher (写) → Kafka (call-events) → MaterializedView → api_call_log (CH)
                                                                   ↓
                                                          trace-svc (读)
                                                                   ↓
                                            ┌─────────────────────┴──────────────────┐
                                            ↓                    ↓                   ↓
                                       list (按 trace_id/api/time)              stats (大盘)
                                       detail (含 token/retry/SSE)
                                            ↓
                                       admin UI / portal UI
```

## Phase 1 范围（基础）

| 功能 | 状态 |
|------|------|
| 调用日志列表（多维过滤） | ✅ |
| 单次调用详情（含 token / latency / retry） | ✅ |
| 聚合统计（成功率 / p50/p95/p99 / qps / top APIs / by hour） | ✅ |
| 跨接口对比（POST /compare） | ⏳ Phase 2 |
| CSV 导出（限 100w 行） | ⏳ Phase 2 |
| Jaeger span 关联（trace_id → span link） | ⏳ Phase 2 |
| 错误堆栈按 trace_id 索引（MinIO） | ⏳ Phase 2 |

## 关键设计

### 1. ClickHouse 没有行级安全（RLS）

PG 的多租户隔离靠 RLS + `SET LOCAL app.tenant_id`。ClickHouse 没有等价机制，所以**所有查询的 WHERE 子句都强制带 `tenant_id`**：

```python
# repository.py - _build_where
if viewer_tenant_id is not None:
    clauses.append("tenant_id = %(tenant_id)s")
    params["tenant_id"] = int(viewer_tenant_id)
```

普通用户调用时 `viewer_tenant_id = ctx.tenant_id`（强制覆盖 query.tenant_id）；超管显式 `use_admin_session=True` 跨租户。

### 2. ClickHouse 客户端在 apihub-core 中共享

```python
# apihub_core/clickhouse.py
def init_clickhouse(settings) -> None: ...   # lifespan 启动
@contextmanager
def ch_session(*, force_tenant_id="sentinel"): ...

def query_all(sql, params, *, force_tenant_id) -> list[dict]: ...
def query_one(sql, params, *, force_tenant_id) -> dict | None: ...
```

`force_tenant_id` 取值：
- `"sentinel"`（默认）→ 取当前 TenantContext；无上下文则抛
- `None` → 超管视角（不强制过滤）
- str → 强制按此 tenant_id 过滤

### 3. CH 不可用时降级

```python
try:
    return ch.query_all(sql, params, force_tenant_id=...)
except RuntimeError as e:
    log.warning("trace_list_clickhouse_unavailable", error=str(e))
    return []  # 或 _empty_stats() 给 0
```

trace 是辅助服务，CH 故障不能阻塞业务。降级返回空列表 / 0 值统计。

### 4. 参数化风格

ClickHouse 用 `%(name)s` 风格（不是 asyncpg 的 `$1`）。所有用户输入都走参数化，不拼 SQL。

### 5. trace_id 查询路径

`GET /v1/trace/calls/{trace_id}` 走 bloom filter 索引（建表时已加），单次查询毫秒级。普通用户额外加 `tenant_id` 双重过滤，防止越权。

## 接口

| 方法 | 路径 | 鉴权 | 说明 |
|------|------|------|------|
| GET  | `/v1/trace/calls` | 同租户 | 列表（支持 api_id/app_id/trace_id/status/since/until/limit/offset） |
| GET  | `/v1/trace/calls/stats` | 同租户 | 聚合统计（成功率 / 分位 / qps / top APIs / by hour） |
| GET  | `/v1/trace/calls/export` | 同租户 | Phase 2 占位 501 |
| GET  | `/v1/trace/calls/{trace_id}` | 同租户 | 单次详情（含 token / retry / SSE） |
| GET  | `/v1/trace/health` | 无 | k8s probe |

**注意**：FastAPI 路由声明顺序 —— `/calls/export` 必须在 `/calls/{trace_id}` 之前，否则被吞。

## 状态过滤

```python
class CallStatusFilter(StrEnum):
    ALL      = "all"      # 不过滤
    SUCCESS  = "success"  # is_success = 1
    FAILED   = "failed"   # is_success = 0
    TIMEOUT  = "timeout"  # is_timeout = 1
```

## 本地开发

```bash
make install
make dev-up              # 起 PG/Redis/Kafka/CH
make run-trace           # uvicorn trace_svc.main:app --port 8008
```

手动测一下：
```bash
# 列表（最近 1 小时）
curl -s 'localhost:8008/v1/trace/calls?since=2026-07-03T15:00:00' \
  -H 'X-API-Key: ak_test' | jq

# 详情
curl -s localhost:8008/v1/trace/calls/tr_abc -H 'X-API-Key: ak_test' | jq

# 大盘统计
curl -s 'localhost:8008/v1/trace/calls/stats?since=2026-07-03T00:00:00&until=2026-07-03T23:59:59' \
  -H 'X-API-Key: ak_test' | jq
```

## 测试

```bash
cd services/services/trace
pytest tests/ -v
# 25 tests, all pass
```

覆盖：
- `test_repository.py`（15）—— `_build_where`（viewer 强制 / 状态过滤 / 全字段 / 非数字 tenant）/ list_calls（含 CH 不可用降级）/ get_call（found/404/tenant 隔离）/ stats（聚合 / CH 不可用 / qps 时间窗口）
- `test_routes.py`（10）—— 端点权限矩阵（admin vs 普通用户）+ query 参数解析 + 404 + export 501 + health

mock 策略：
- ClickHouse：`fake_ch` fixture monkeypatch `ch.query_all` / `query_one` 记录所有调用
- 鉴权：`as_normal_user` fixture 切换普通用户上下文

## 性能预算（prod）

- 3 副本（无状态、可水平扩展）
- 单副本 1 CPU / 1Gi（聚合查询偶发并发，CH 直查不重）
- HPA 基于 CPU 70%
- CH 查询超时 30s（`send_receive_timeout`）

## 关联

- 上游：admin UI（大盘 / 调用列表）/ portal UI（开发者查自己应用的调用）
- 数据源：api_call_log 表（ClickHouse，dispatcher 通过 Kafka MaterializedView 写入）
- 下游：无（trace 是只读消费方）
