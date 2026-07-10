# Phase 3 P1 验证补齐 —— traceparent 贯通 / cross-ns / workflow stub e2e

> 设计文档（brainstorming 产物）。实现计划由 writing-plans 接续生成。

## Goal

把 Phase 3 的三项「事实 P0」在现有 kind 集群上补齐并端到端验证：
1. **traceparent 贯通** —— dispatcher → Kafka → executor 的 W3C trace 上下文真连通（**勘误**：实际断点是 executor 未接 OTel + `_call_backend` 未注入 W3C，消费侧早已通——见下「背景」）。
2. **cross-ns DNS** —— 显式断言并记录跨 namespace 解析现状。
3. **workflow stub e2e** —— 按文档 §4 接通 dispatcher → workflow-svc（stub 模式），经 APISIX 端到端跑通「提交 + 轮询」。

## Architecture / 背景（已探查确认）

- **traceparent 生产端已就绪**：`apihub_core.kafka.emit`（kafka.py:55）在发消息前 `_PROPAGATOR.inject(carrier)` 注入 W3C traceparent，并起 `kafka.produce` PRODUCER span（kafka.py:68-94）。`apihub_core.kafka.consume_with_trace`（kafka.py:177）/ `consume_span`（kafka.py:123）消费侧还原 carrier、起 CONSUMER span 并 `attach` 上下文。
- **真正的断点有两个（勘误：消费侧早已包好）**：`executor/consumer.py:73` **已经**把 `_handle`（内含 `process_task`）包在 `consume_with_trace` 里，消费侧 traceparent 还原本就是通的。实际缺的是：(1) `executor/main.py` 是唯一没走 `apihub_core.create_app` 的服务（手搓 `FastAPI()`），tracer 为 NoOp → executor 根本不产 span，Jaeger 里看不到 executor；(2) `_call_backend` 只发自定义 `X-Trace-Id`（processor.py:129），没注入 W3C `traceparent` → OTel 链不延续到业务 backend。
- **`_call_backend` 只发自定义 header**：processor.py:124-130 给 backend 的 header 里有 `X-Trace-Id`（line 129），没有 W3C `traceparent`，OTel 链不延续到 backend。
- **workflow-svc 已完整实现**：`StubArgoClient`（内存状态机）+ `K8sArgoClient`（打 Argo CRD）双实现；路由 `POST /v1/workflows`、`GET /v1/workflows/{id}`（含实时状态+steps）、list/cancel/resume/steps/logs 全套（workflow_svc/routes.py）。kind 用 `argo_mode=stub`。
- **dispatcher 对 workflow 抛 501**：dispatcher/routes.py:53-58，`backend_type=="workflow"` 分支直接 501；且无文档 §4 要求的 `POST /v1/jobs` 入口。
- **文档 §4（05-core-flows.md:214-244）规定**：调用方 `POST /v1/jobs`（mode: workflow）→ dispatcher → workflow-svc 提交 Argo → `GET /jobs/{id}` 轮询进度+步骤。
- **cross-ns 现状**：namespaces.yaml 有 5 个 ns（apihub-system/data/ingress/monitoring/workflow）；数据层走 host docker-compose（外部 `__HOST_IP__`），业务服务全在 `apihub-system`；唯一真实跨 ns 调用 = APISIX(`apihub-ingress`) → dispatcher(`apihub-system`)，已在 PR #8 Stage 3 验绿。

## Tech Stack

Python 3.11 / FastAPI / asyncpg / aiokafka / OpenTelemetry（1.40.0 + instrumentation 0.61b0）/ Jaeger（host compose，:16686）/ APISIX（kind NodePort 30080）/ kind K8s。

---

## Global Constraints（每项任务的隐含要求）

- **分支**：`feat/phase3-p1-validation`，off `main`（当前 `0e4e320`）。
- **环境复用**：复用**当前在线**的 kind 集群（context `kind-apihub`，12 pods）+ host docker-compose 数据层（PG/Redis/Kafka/CH/MinIO/Jaeger/OTel）。不重建集群。改动了服务代码需 rebuild 对应镜像 + `kubectl rollout restart`。
- **顺序**：A（traceparent）→ B（cross-ns）→ C（workflow）。每项独立 commit。
- **密码/账号**：业务账号 `apihub_app` / `apihub_app_dev_pwd`（**非** superuser `apihub` 的 `apihub_dev_pwd`）。CI Redis 密码 `apihub_dev_pwd`。
- **依赖版本锁**：不引入新版本漂移；OTel 保持 1.40.0 + 0.61b0 配对。
- **多租户 RLS 不破坏**：executor 改动只动 trace 上下文包装，不碰 `db_session()`/`SET LOCAL app.tenant_id` 路径。workflow dispatcher 代理走鉴权后的 TenantContext。
- **seed 数据**：tenant `tenant_a`，API key `ak_test_a_demo001`（APISIX key-auth 用）。
- **stub 行为约束**：`StubArgoClient` submit 后状态恒为 `RUNNING`，无后台 tick 推进；steps 由 `_derive_steps` 从 `spec.templates[].name` 派生。断言只能验 `running` + steps 存在，**不能**验 `succeeded`。

---

## A. traceparent 贯通

### A.1 代码改动

1. **`executor/main.py`**（勘误后的真正改动）：executor 是唯一没走 `apihub_core.create_app` 的服务（手搓 `FastAPI()`），tracer 为 NoOp、Jaeger 里看不到它。补 `configure_tracing()` + `FastAPIInstrumentor.instrument_app(app)`，与其它服务一致。**`executor/consumer.py` 无需改动**——`consumer.py:73` 早已把 `_handle`（含 `process_task`）包在 `consume_with_trace` 里，消费侧 traceparent 还原 + CONSUMER span 本就是通的。
2. **`executor/processor._call_backend`**（processor.py:124-130）：在现有 headers 基础上注入 W3C traceparent，让 OTel 链延续到业务 backend：
   ```python
   from opentelemetry import propagate
   tp: dict[str, str] = {}
   propagate.inject(tp)  # 当前 context（由 consume_with_trace attach）注入
   headers.update(tp)
   ```
   保留既有 `X-Trace-Id`（向后兼容）。

### A.2 断言 —— 新增 `scripts/smoke/k8s-traceparent.py`

- 触发 L2 异步任务（复用 k8s-links.py 已验证的 dispatcher→Kafka→executor 路径）：经 APISIX `POST /dispatch/<async-api-path>`（`X-API-Key: ak_test_a_demo001`），拿 task_id。
- 等待 span 导出：`time.sleep(10)`（OTel `BatchSpanProcessor` 默认 ~5s 批量导出，留余量）。
- 查 Jaeger API：`GET http://<jaeger-host>:16686/api/traces?service=dispatcher&limit=20&lookback=1h`（Jaeger 在 host compose；host 从 `docker port apihub-jaeger` 或 compose 固定端口取，实现者确认）。
- 断言：在返回的 traces 中存在**至少一条 trace**，其 spans 同时满足：
  - 含 `serviceName=dispatcher` 的 SERVER span（`operationName` 形如 `POST /dispatch/...`）；
  - 含 `serviceName=executor`、`operationName=kafka.consume task-requests` 的 CONSUMER span；
  - 二者 `traceID` 相同。
- 失败时打印找到的 trace 数量与各 span 的 service/operation，便于排查。
- 退出码：成功 0，断言失败 1，环境不可用 2。

### A.3 验收

- 上述 smoke 退出 0。
- 改动前同样查询应找不到连通 trace（可作 before/after 对照，非强制）。

---

## B. cross-ns DNS

### B.1 改动

- **`scripts/smoke/k8s-links.py`**（或新增 `scripts/smoke/k8s-crossns.py`，推荐并入 k8s-links 末尾新 stage）：增加一条显式断言 —— 在 `apihub-ingress` 视角解析 `dispatcher.apihub-system` 成功（即 APISIX → dispatcher 这条已绿路径显式 assert，而非隐式 200）。实现：复用 APISIX `/dispatch/*` 200 那一步，附加断言「响应确实来自 dispatcher」（如响应体含 dispatcher 特征字段），并 log 明确「cross-ns apihub-ingress→apihub-system 解析 OK」。

### B.2 文档

- **`docs/phase2-integration-findings.md`** Phase 3 P1 该项：从待办改为「已验证（当前布局）」，注明：数据层外部（host compose）→ 服务间无跨 ns 数据调用；唯一跨 ns（ingress→system）已验；**待数据服务（PG/Redis/Kafka/CH/MinIO）迁入 `apihub-data` in-cluster 后需重验**。

### B.3 验收

- k8s-links（含新断言）退出 0；findings 已更新。

---

## C. workflow stub e2e

### C.1 dispatcher 改动 —— 新增 `/v1/jobs`（按文档 §4）

- **`dispatcher/routes.py`**：新增
  - `POST /v1/jobs`（mode: workflow）：请求体 `{api_id: int, app_id: int, spec: dict, trace_id?: str, namespace?: str}`。从当前 OTel context 取 trace_id（缺则生成），HTTP 代理到 workflow-svc `POST /v1/workflows`（`WORKFLOW_SERVICE` URL，默认 `http://workflow.apihub-system`），透传 `X-API-Key`/TenantContext 需要的鉴权头。返回 workflow-svc 的 `Workflow`（201）。
  - `GET /v1/jobs/{job_id}`：代理 workflow-svc `GET /v1/workflows/{job_id}`，返回 `WorkflowDetail`（含 status + steps）。
- **dispatcher configmap**（base + kind overlay）：加 `WORKFLOW_SERVICE=http://workflow.apihub-system`。
- **dispatcher 501 分支**：routes.py:53-58 的 `backend_type=="workflow"` 501 保留（`/dispatch` catch-all 仍不接受 workflow；workflow 走独立 `/v1/jobs`，与文档一致）。加注释指明 workflow 入口为 `/v1/jobs`。

### C.2 APISIX 路由

- **`scripts/kind/apisix-setup.sh`**：新增 route `/v1/jobs/*` → `dispatcher.apihub-system:80`，启用 key-auth（`X-API-Key`），与既有 `/dispatch/*` 并列。

### C.3 断言 —— 新增 `scripts/smoke/k8s-workflow.py`

- 经 APISIX `POST http://127.0.0.1:30080/v1/jobs`，header `X-API-Key: ak_test_a_demo001`，body：
  ```json
  {
    "api_id": <seeded tenant_a api_id>,
    "app_id": <seeded tenant_a app_id>,
    "spec": {
      "entrypoint": "main",
      "templates": [
        {"name": "main", "steps": [[{"name": "s1", "template": "echo"}], [{"name": "s2", "template": "echo"}]]},
        {"name": "echo", "container": {"image": "busybox", "command": ["echo", "hi"]}}
      ]
    }
  }
  ```
  （`api_id`/`app_id` 取自 `scripts/init-db/02-seed.sql` 中 tenant_a 的现有行；实现者确认具体 id。）
- 断言：HTTP 201，响应含 `id`（workflow_id）与 `status`。
- `GET /v1/jobs/{id}`：断言 200，`status == "running"`，`steps` 长度 ≥ 2（StubArgoClient 从 templates 派生 main+echo；具体取决于 _derive_steps 实现，断言 ≥1 且非空）。
- 退出码：成功 0，失败 1，环境不可用 2。

### C.4 验收

- k8s-workflow smoke 退出 0。
- `kubectl -n apihub-system logs deploy/dispatcher` 与 `deploy/workflow` 能看到对应请求链路。

---

## Out of Scope（拆下轮）

- 真装 Argo Workflow CRD + controller 到 kind，验 `K8sArgoClient`（提交真 CRD、Pod 跑 step）。
- MinIO 产物上传/下载 e2e。
- workflow cancel/resume/logs(SSE) 的 e2e。
- 数据服务（PG/Redis/Kafka/CH/MinIO）迁入 `apihub-data` in-cluster 后的 cross-ns 重验。
- `_call_backend` 之外其余 backend 自定义 header 的 OTel 化。

## Testing Strategy

- 三项各配一个 smoke 脚本（A: k8s-traceparent.py，B: 并入 k8s-links.py，C: k8s-workflow.py），在 kind 集群实跑，退出码即验收。
- executor 改动补/调单测：`executor/tests/` 增加「consumer 经 consume_with_trace 后，process_task 内活跃 span 的 trace_id 与 msg headers traceparent 一致」（用 fake msg + in-memory tracer 断言 context 传播）。
- dispatcher `/v1/jobs` 代理补单测：mock workflow-svc 响应，断言透传与状态码映射。
- 不新增 CI workflow（smoke 依赖 kind 集群，本地/手动跑；与既有 k8s-links.py 一致）。

## Risks

- **Jaeger 查询时序**：BatchSpanProcessor 异步导出，smoke 需足够等待（10s）+ 重试轮询，避免 span 未到。
- **Jaeger 端口/服务发现**：host compose 里 Jaeger 的 query 端口需确认（实现者用 `docker port apihub-jaeger` 回读，仿 bootstrap 处理 Redis 端口的做法）。
- **StubArgoClient 恒 RUNNING**：断言不能用 succeeded，只能用 running+steps。
- **seed api_id/app_id**：必须属 tenant_a 且 RLS 可见；若 seed 无合适行，smoke 自行 INSERT 一条 workflow-type api（ON CONFLICT DO NOTHING）。
- **APISIX route 顺序**：`/v1/jobs/*` 与 `/dispatch/*` 都是通配，需确认 APISIX 匹配优先级不冲突（不同前缀，应无冲突）。
