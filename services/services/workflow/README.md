# workflow-svc

> 长时 DAG 任务调度服务 —— 封装 Argo Workflow（K8s CRD）。
> 详见 [docs/03-services.md §3.4](../../../docs/03-services.md)。

## 架构

```
admin UI / portal UI
       │  POST /v1/workflows (api_id, app_id, trace_id, spec)
       ↓
workflow-svc ──→ PG workflow_instance (tenant_id, uuid, argo_name, status, spec)
       │
       ↓
K8s API server ──→ Argo Workflow CRD (apihub-workflows namespace)
       │
       ↓
Argo controller 起pod跑 DAG ──→ MinIO（产物 / 日志）

后续：
  GET /workflows/{id}       ──→ 同步 Argo phase + status.nodes
  POST /{id}/cancel         ──→ PATCH spec.shutdown=Stop
  POST /{id}/resume         ──→ POST /resume 子资源
  GET /{id}/steps           ──→ 读 status.nodes
  GET /{id}/logs (SSE)      ──→ stream workflow log endpoint
```

## Phase 2 范围

| 功能 | 状态 |
|------|------|
| POST /v1/workflows 提交（dev stub / prod k8s 双模式） | ✅ |
| GET /v1/workflows 列表（多维过滤） | ✅ |
| GET /v1/workflows/{id} 详情（实时同步 Argo） | ✅ |
| POST /{id}/cancel / resume | ✅ |
| GET /{id}/steps | ✅ |
| GET /{id}/logs SSE 流式日志 | ✅ |
| Argo WorkflowTemplate 库（预定义 DAG） | ⏳ Phase 3 |
| MinIO 产物管理（下载 / 列表） | ⏳ Phase 3 |
| Workflow 版本管理（关联 api_version） | ⏳ Phase 3 |

## 关键设计

### 1. 双模式 Argo client

dev / test 没有真 K8s 集群，用 `StubArgoClient` 内存模拟；
prod 用 `K8sArgoClient` 走 K8s API server，CRD endpoint：
`/apis/argoproj.io/v1alpha1/namespaces/{ns}/workflows`。

```python
# main.py
mode = settings.argo_mode  # "stub" / "k8s"
argo_client.init_argo_client(mode=mode)
```

stub 实现：内存 dict 存 workflow 状态，cancel/resume/get_steps/stream_logs 全模拟。
K8s 实现：用 httpx + pod SA token 调 K8s API（不引 kubernetes python client，避免重依赖）。

### 2. PG 表 workflow_instance

Argo 是 workflow 真源；PG 只是索引（tenant_id ↔ argo_name 映射 + 列表查询）。
读时双查：先查 PG 拿 argo_name，再查 Argo 拿实时状态，回写 PG（best-effort）。

```sql
CREATE TABLE workflow_instance (
    tenant_id       BIGINT NOT NULL,
    id              BIGSERIAL PRIMARY KEY,
    workflow_uuid   VARCHAR(64) NOT NULL UNIQUE,
    argo_name       VARCHAR(128) NOT NULL,
    namespace       VARCHAR(64) NOT NULL DEFAULT 'apihub-workflows',
    api_id          BIGINT,
    app_id          BIGINT,
    trace_id        VARCHAR(64),
    spec            JSONB NOT NULL,
    status          VARCHAR(20) NOT NULL DEFAULT 'submitted',
    message         TEXT,
    submitted_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### 3. RBAC（仅 prod K8s 模式）

workflow-svc 的 ServiceAccount 需要 Role / RoleBinding 操作 Argo CRD：

```yaml
# deploy/k8s/services/workflow/deployment.yaml 已包含
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: workflow-argo-role
  namespace: apihub-workflows  # Argo CRD 所在 namespace
rules:
  - apiGroups: ["argoproj.io"]
    resources: ["workflows", "workflowtemplates"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
  - apiGroups: ["argoproj.io"]
    resources: ["workflows/log"]
    verbs: ["get"]
```

注意：workflow-svc 在 `apihub-system`，但要操作 `apihub-workflows` namespace 的 CRD →
Role + RoleBinding 跨 namespace（RoleBinding subject 引用 system namespace 的 SA）。

### 4. 路由声明顺序

`/v1/workflows/health` 必须在 `/v1/workflows/{workflow_id}` 之前，
否则 `health` 被当 int 参数吞（422）。同 trace-svc / retry-svc 的坑。

### 5. Argo phase 映射

```python
PHASE_MAP = {
    "Pending":  WorkflowStatus.SUBMITTED,
    "Running":  WorkflowStatus.RUNNING,
    "Succeeded": WorkflowStatus.SUCCEEDED,
    "Failed":   WorkflowStatus.FAILED,
    "Error":    WorkflowStatus.FAILED,
    "Skipped":  WorkflowStatus.CANCELLED,
    "Stopped":  WorkflowStatus.CANCELLED,
}
```

### 6. 日志 SSE

```python
# routes.py - stream_logs
async def _gen():
    async for line in client.stream_logs(...):
        chunk = LogChunk(step_name=..., line=line, timestamp=now())
        yield f"data: {chunk.model_dump_json()}\n\n".encode()
return StreamingResponse(_gen(), media_type="text/event-stream")
```

stub 一次性 yield 所有行；K8s 模式用 `httpx.stream` 持续读 Argo log endpoint。

## 接口

| 方法 | 路径 | 鉴权 | 说明 |
|------|------|------|------|
| POST | `/v1/workflows` | 同租户 | 提交（spec / api_id / app_id / trace_id） |
| GET  | `/v1/workflows` | 同租户 | 列表（api_id/app_id/trace_id/status/since/until/limit/offset） |
| GET  | `/v1/workflows/{id}` | 同租户 | 详情（实时同步 Argo） |
| POST | `/v1/workflows/{id}/cancel` | 同租户 | 取消（PATCH shutdown=Stop） |
| POST | `/v1/workflows/{id}/resume` | 同租户 | 恢复 |
| GET  | `/v1/workflows/{id}/steps` | 同租户 | 步骤详情 |
| GET  | `/v1/workflows/{id}/logs` | 同租户 | SSE 流式日志（?step_name=） |
| GET  | `/v1/workflows/health` | 无 | k8s probe |

## 本地开发

```bash
make install
make dev-up              # 起 PG/Redis/Kafka/CH/MinIO
ARGO_MODE=stub make run-workflow    # uvicorn workflow_svc.main:app --port 8010
```

手动测一下：

```bash
# 提交
curl -s -XPOST localhost:8010/v1/workflows \
  -H 'X-API-Key: ak_test' -H 'Content-Type: application/json' \
  -d '{
    "api_id": 100, "app_id": 200, "trace_id": "tr_abc",
    "spec": {
      "entrypoint": "main",
      "templates": [{"name": "main", "script": {"image": "python:3.11", "source": "print(1)"}}]
    }
  }' | jq

# 列表
curl -s 'localhost:8010/v1/workflows' -H 'X-API-Key: ak_test' | jq

# 详情（含 steps）
curl -s localhost:8010/v1/workflows/1 -H 'X-API-Key: ak_test' | jq

# 取消
curl -s -XPOST localhost:8010/v1/workflows/1/cancel -H 'X-API-Key: ak_test' | jq

# 日志流
curl -N localhost:8010/v1/workflows/1/logs -H 'X-API-Key: ak_test'
```

## 测试

```bash
cd services/services/workflow
pytest tests/ -v
# 27 tests, all pass
```

覆盖：
- `test_argo_client.py`（12）—— stub submit / status / cancel / resume / logs / 单 step；
  factory init / mode 校验 / phase 映射
- `test_routes.py`（15）—— health / submit 成功 / submit Argo 失败 502 /
  get 404 / get 详情含 steps / cancel / resume / steps 404 / logs SSE 格式 /
  list 空 / list 命中

mock 策略：
- repository：`stub_repo` fixture 替换所有 PG 操作，维护 in-memory dict
- argo client：`StubArgoClient` 内存模拟 Argo（dev/prod 双模式隔离的关键）
- 鉴权：`_noop_auth` 注入超管 TenantContext

## 性能预算（prod）

- 3 副本（admin UI 偶发查询 + SSE 日志流，2 副本最低保障）
- 单副本 1 CPU / 1Gi（HTTP 转发 + JSON 序列化，CPU 偶发）
- K8s API 超时 30s（read）/ 5s（connect）
- 计算资源消耗在 Argo pod 里，不在 workflow-svc

## 关联

- 上游：admin UI / portal UI（长任务后台）；dispatcher（API 模式 = workflow 时
  通过 gRPC 转发到 workflow-svc，目前 Phase 2 走 HTTP 直提）
- 依赖：PG (workflow_instance)、Argo Workflow CRD、MinIO（产物 / 日志）
- 下游：Argo controller（K8s in-cluster）
