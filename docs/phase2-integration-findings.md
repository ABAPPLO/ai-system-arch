# Phase 2 端到端联调 —— 暴露的集成层问题清单

时间：2026-07-07 ~ 07-08
范围：dev 栈全量起 + auth-svc 联调，发现并修复的 12 个集成层问题。

每条都按"症状 / 根因 / 修复 / 验证"记录，给 Phase 2 生产化收尾提供同类坑的查找线索。

---

## 已修复（本会话）

### 1. RLS 不生效 —— superuser 绕过租户隔离

- **症状**：`SET LOCAL app.tenant_id='tenant_a'; SELECT count(*) FROM api;` 返回 3（全表），应该是 2。
- **根因**：`POSTGRES_USER=apihub` 默认是 superuser + BYPASSRLS。PostgreSQL 规则：**superuser 永远绕过 RLS**，`FORCE ROW LEVEL SECURITY` 只让 owner 受策略约束，对 superuser 无效。
- **修复**：
  - 新增 `scripts/init-db/00-roles.sql` 创建业务账号 `apihub_app`（NOSUPERUSER NOBYPASSRLS）
  - 新增 `scripts/init-db/99-grants.sql` 把全部表/序列显式 GRANT 给业务账号
  - `.env.dev` 改 `PG_USER=apihub_app`，业务服务全用这个账号
- **验证**：tenant_a 见 2 个 api，tenant_b 见 1 个，`is_platform_admin=on` 见 3 个。RLS 矩阵全部通过。

### 2. docker-compose `${PG_USER:-apihub}` 把 superuser 名改成业务账号

- **症状**：compose 起来后 `apihub` 角色不存在，`apihub_app` 反而成了 superuser。
- **根因**：`.env.dev` 里 `PG_USER=apihub_app` 被 compose 替换进 `POSTGRES_USER: ${PG_USER:-apihub}`，把 superuser 名错改了。
- **修复**：`docker-compose.dev.yml` 里把 `POSTGRES_USER` / `POSTGRES_DB` 硬编码为 `apihub`，注释说明不能从 env 读。

### 3. CREATE OR REPLACE POLICY 不被支持

- **症状**：`make dev-reset` 报语法错误。
- **根因**：PostgreSQL 没有 `CREATE OR REPLACE POLICY`（POLICY 不像 FUNCTION/VIEW 那样支持 replace）。
- **修复**：所有 POLICY 改成 `DROP POLICY IF EXISTS ... ; CREATE POLICY ...` 模式（20+ 处）。

### 4. 缺 FORCE ROW LEVEL SECURITY

- **症状**：业务账号即使不是 superuser，连过去仍能看见全表。
- **根因**：表 owner 绕过 RLS（默认行为），业务账号恰好是 owner。
- **修复**：所有租户感知的表加 `ALTER TABLE ... FORCE ROW LEVEL SECURITY`。

### 5. ID 类型漂移（change_request / retry_*）

- **症状**：admin-bff / api-registry / retry-svc 在 ID 类型上 int / text 混用。
- **根因**：早期 schema 用 `text`，部分服务代码用 `int` 接收。
- **修复**：
  - 数据库保持 `text`/`bigserial`（前者实体，后者流水）
  - Pydantic 模型全部改 `str`，并加 `coerce_numbers_to_str=True`（HTTP 边界宽松）
  - repository 层 `_row_to_*` 函数去掉 `int()` 强转

### 6. api_key seed 用占位 hash

- **症状**：seed 里的 hash 是占位字符串，auth-svc 永远校验不过。
- **修复**：用真实 sha256(ak_test_a_demo001) / sha256(ak_test_b_demo001) / sha256(ak_test_ext_x_demo) 替换。

### 7. Kafka listeners 缺 EXTERNAL

- **症状**：本地代码连 `localhost:9094` 拒绝。
- **根因**：`KAFKA_CFG_LISTENERS` 只有 PLAINTEXT+CONTROLLER，没声明 EXTERNAL。
- **修复**：加 `EXTERNAL://:9094` 到 LISTENERS + protocol map。

### 8. ClickHouse 9000 端口和 MinIO 冲突

- **症状**：MinIO 和 CH 都想绑 9000。
- **修复**：CH native 改成 `19000:9000`（clickhouse_connect 默认走 HTTP/8123，不影响）。

### 9. ClickHouse Kafka engine 不支持 DEFAULT

- **症状**：`make dev-reset` 后 CH 容器退出，Code 36 `KafkaEngine doesn't support DEFAULT/MATERIALIZED/EPHEMERAL`。
- **根因**：`api_call_events_src` 表给 KafkaEngine 加了 `DEFAULT ''` 之类的列约束，CH 24.3 不支持。
- **修复**：去掉 Kafka source 表所有 DEFAULT；可选默认值在 Materialized View 里 `COALESCE` 或者由生产端 JSON 里就带。

### 10. ClickHouse VALUES 里写 `now() - INTERVAL 1 HOUR` 触发 SYNTAX_ERROR

- **症状**：CH init 在测试数据 INSERT 处退出。
- **根因**：CH 的 VALUES 解析器不像 PG 那样自由解析表达式。
- **修复**：注释掉测试数据 INSERT，需要时改用 `INSERT ... SELECT now() - INTERVAL 1 HOUR, ...`。

### 11. OpenTelemetry FastAPIInstrumentor API 漂移

- **症状**：`FastAPIInstrumentor.instrument()` 报 `missing 1 required positional argument: 'self'`。
- **根因**：OTel 0.40+ 把 `instrument()` 改成实例方法。
- **修复**：`services/libs/apihub-core/src/apihub_core/tracing.py` 改成 `FastAPIInstrumentor().instrument()`。

### 12. asyncpg 硬编码 `ssl="require"` + CH env var 不匹配

- **症状**：auth-svc 启动 `rejected SSL upgrade`，CH `default user password incorrect`。
- **根因**：
  - dev 容器 PG 没装 SSL，但 `init_pool` 强制 ssl=require
  - Settings 字段是 `ch_username`，`.env.dev` 里写的是 `CH_USER`（不匹配）
- **修复**：
  - `Settings.pg_ssl: str = "disable"`，`init_pool` 透传
  - `.env.dev` 改 `CH_USERNAME=apihub`，并加 `PG_SSL=disable`

### 13. auth.py 期望 `resp.json()["data"]` envelope

- **症状**：api-registry 调 auth-svc verify 后报 `KeyError: 'data'`。
- **根因**：`apihub_core/auth.py` 假设 auth-svc 返回 `{data: {...}}`，实际 auth-svc 直接返回 `VerifyResponse` flat。
- **修复**：直接读 `resp.json()`，不再解 `["data"]`。

### 14. dispatcher/forwarder.py 写错字节字面量

- **症状**：dispatcher 启动 `SyntaxError: invalid syntax`，`yield _b"data: ..."`。
- **根因**：`_b` 不是合法 prefix（应该是 `b`）。
- **修复**：改成 `b"data: ..."`。

### 15. admin-bff / retry-svc 把 K8s 集群内 DNS 写死

- **症状**：admin-bff dashboard 调下游报 `Name or service not known: tenant.apihub-system`。
- **根因**：`aggregator.py` 把 `TENANT_SVC_URL = "http://tenant.apihub-system/..."` 写死，`retry/worker.py` 同样写死 executor URL。
- **修复**：抽到 `Settings.tenant_service_url` / `executor_service_template`，`.env.dev` 覆盖到 localhost。

### 16. asyncpg jsonb 列默认返回 str

- **症状**：tenant-svc list tenants 报 `Input should be a valid dictionary [type=dict_type, input_value='{"dept": "risk"}']`。
- **根因**：asyncpg 不像 psycopg 自动解析 jsonb，默认当 text 返回；Pydantic 模型要 dict。
- **修复**：`init_pool` 传 `init=_init_jsonb_codec`，给每个新连接注册 jsonb/json codec → `json.loads`。

### 17. trace-svc SQL 用旧 schema 列名（未修，已知 tech debt）

- **症状**：`/v1/trace/calls` → CH 报 `Unknown expression identifier 'api_uuid'`，`tenant_id = 0`。
- **根因**：trace-svc 的 repository SQL 还在用 `api_uuid / app_uuid / api_path / api_method / app_name / caller_ip / http_status / is_timeout / error_type`，但 `01-schema.sql` 里的 `api_call_log` 表用的是 `api_id / app_id / path / method / status_code / is_success / error_code / error_msg`。同时 tenant_id 被强转 int。
- **修复**：不在本会话内修，列入 Phase 2 生产化收尾 P1。

### 18. api_version 缺 method/path 列（INSERT NotNullViolation）

- **症状**：声明式 CLI apply 报 `null value in column "method" of relation "api_version" violates not-null constraint`。
- **根因**：`api_registry/models.py::ApiVersionCreate` 没有 `method` / `path` 字段，`api_registry/routes.py::create_version` 的 INSERT 也没把这两列写进去，但 schema (`01-schema.sql` 第 128-129 行) 把它们标了 NOT NULL。
- **修复**：
  - `models.py`：加 `Method` StrEnum + `method: Method = Method.GET`、`path: str` 字段，`ApiVersionResponse` 同步加这两个字段。
  - `routes.py`：INSERT 加 `method, path` 两列、`$7, $8` 两个占位符、对应 args。

### 19. change_request jsonb 双重序列化（codec + json.dumps + ::text + json.loads）

- **症状**：apply 流程走完 INSERT 后查回 change_request，pydantic 校验报 `proposed_config Input should be a valid dictionary [type=dict_type, input_value='{"api": ...}', input_type=str]`。
- **根因**：`change_request.py` 在 `_init_jsonb_codec` 已注册的池上仍然 `json.dumps(req.proposed_config)` 写库（双重序列化：codec encoder 再 encode 一次 string），SELECT 用 `proposed_config::text` 强制变 text 绕开 codec 解码，`_row_to_request` 又 `json.loads` 还原。任一环节漏改都会爆。
- **修复**：codec 已经把 jsonb 当 dict 自动编码/解码，所以全部去掉手工转换：
  - INSERT 传 `req.proposed_config` (dict) / `current_config` (dict|None)，不再 `json.dumps`。
  - SELECT 去掉 `::text`。
  - `_row_to_request` 直接 `proposed = row["proposed_config"]`、`proposed_config=proposed if proposed else {}`，不再 `json.loads`。
  - 顺便删了 `import json`。

### 20. api_change_request / retry_task 误挂 updated_at 触发器（NEW 没字段）

- **症状**：dev 自助 apply 走到 `mark_applied` 时 UPDATE api_change_request 报 `record "new" has no field "updated_at"`。
- **根因**：`03-phase2.sql` 给 `api_change_request` 和 `retry_task` 都挂了 `set_updated_at` 触发器（`NEW.updated_at = NOW()`），但这俩表压根没有 `updated_at` 列。注释里写"retry_attempt / audit_log 之类没有 updated_at，不挂触发器"，意图是对的，只是漏排除了这俩。
- **修复**：`03-phase2.sql` 改成对这两张表 `DROP TRIGGER IF EXISTS set_updated_at`。已对 dev DB 直接 `DROP TRIGGER` 修过，下次 `make dev-reset` 也会跑新 SQL。

### 21. retry repository 同样的 jsonb 双重序列化

- **症状**：和 #19 一样的 pattern，retry-svc 在 `_init_jsonb_codec` 已注册的池上仍然 `json.dumps(original_request)` 写库 + `original_request::text` 读 + `json.loads()` 还原。任一环节漏改都会让 worker 拿到 str 而不是 dict，然后 `detail.original_request.get("backend_url", "")` 报 `AttributeError: 'str' object has no attribute 'get'`。
- **修复**：`retry/repository.py` 7 处全部去掉手工转换：
  - `create_retry_task` 写 `original_request` 直接传 dict
  - 3 个 SELECT 的 `original_request::text` 改成 `original_request`
  - `get_retry_task` 的 `_row_to_request` 直接用 `row["original_request"]`
  - `_insert_attempt` 写 `request_body` / `response_body` 直接传 dict
  - `_row_to_attempt` 直接用 row 字段
  - retry_attempt SELECT 去掉 `request_body::text` / `response_body::text`
  - 顺便删了 `import json`

### 22. executor 缺 `/v1/internal/retry` 端点

- **症状**：retry worker 调 `POST http://executor:8003/v1/internal/retry` 全部 404，所有重试请求第一轮就走 dead-letter。
- **根因**：executor 的 `main.py` 只有 `/health/*` 和 `/metrics`，没有 retry 入口。worker 那边代码早就写好了，但接收端没人实现。
- **修复**：executor `main.py` 加 `RetryRequest` 模型 + `POST /v1/internal/retry` 端点：
  - 复用 `processor._client` 单例
  - POST `req.backend_url`，按 status 映射 `succeeded` / `backend_unreachable` / `backend_http_{code}`
  - 不写 PG（retry_task 状态机由 retry-svc 维护）

### 23. retry worker 用 HTTP status 判定成功（最致命）

- **症状**：executor 调失败的后端返回 `{"succeeded": false, "error_code": "backend_unreachable"}`，HTTP 状态 200。worker 把它判定为「重试成功」，直接 `mark_succeeded` 把 retry_task 改成 succeeded，retry_count=0。结果就是任何 backend 故障都被吞掉，retry 链形同虚设。
- **根因**：`retry_svc/worker.py::_call_executor` 末尾 `ok = 200 <= resp.status_code < 300` —— 把"executor 这跳 HTTP 通不通"当成了"重试业务成功"。但 executor 的约定是：HTTP 永远 200，真正信号在 body 的 `succeeded` 字段。
- **修复**：worker 改成读 `body["succeeded"]` 字段；body 没有 `succeeded` 字段时报 `executor_bad_response_{status}`；同时把 body 里的 `status` / `body` / `error_code` / `error_msg` / `latency_ms` 透传出去（之前是从 HTTP status 编 `error_code`，和 executor 实际返回的 error_code 对不上）。

---

## 验证状态（截至 2026-07-08）

- ✅ Dev 栈全部健康（PG / Redis / Kafka / CH / MinIO / Jaeger / OTel / Prom / Grafana）
- ✅ PG schema（12 表）+ 种子数据 + RLS 矩阵全部通过
- ✅ 全部 8 个业务服务起来：
  - auth (8002) — APIKey verify 通过真实 key
  - api-registry (8100) — /v1/apis RLS 隔离生效（tenant_a 见 2 个 / tenant_b 见 1 个 / admin 见全部）
  - tenant (8005) — /v1/tenant/tenants 返回 3 个租户
  - admin-bff (8006) — /v1/admin/dashboard 聚合成功（tenants total=3 active=3）
  - dispatcher (8101) — 启动正常
  - executor (8003) — 启动正常
  - retry (8009) — 启动正常，消费 task-failures topic
  - trace (8008) — 启动正常，但 SQL 用旧 schema 列名，调 /v1/trace/calls 报错（P1 待修）
- ✅ task #98 完成：声明式 CLI apply 验证通过
  - `apihub-cli apply /tmp/test-product-api.yaml --env dev` 跑通：api_record + api_version（含 method/path）+ api_change_request 一并入库
  - dev 自助路径完整跑完：`pending → approved → applied`（`applied_at` 已写）
  - 期间修了 3 个坑（#18 method/path 缺列 / #19 change_request jsonb 双重序列化 / #20 误挂 updated_at 触发器）
- ✅ task #99 完成：失败重试链路验证通过
  - **失败路径**：往 `task-failures` 注入一条 backend=`http://127.0.0.1:9/x`（连接拒绝）的失败消息 → retry-svc consumer 创建 retry_task（pending）→ worker 调 executor → executor 返回 `succeeded=false` → worker `mark_failed_attempt` + 指数退避重排 → 第 2 次仍失败 → retry_count 达 max_attempts=2 → status=`dead`，retry_attempt 表记 2 条 `backend_unreachable`。
  - **成功路径**：起一个本地 OK HTTP server，注入 backend 指向它的失败消息 → retry_task#4 第一次重试就 200 → status=`succeeded`。
  - 期间修了 4 个坑（#21 retry jsonb 双重序列化 / #22 executor 缺 `/v1/internal/retry` / #23 worker 用 HTTP status 判定成功）。
- ✅ task #100 完成：Jaeger traceparent 端到端贯通
  - **完整链路**：CLIENT traceparent → api-registry SERVER span → CLIENT httpx 调 auth-svc → auth-svc SERVER span → asyncpg SELECT / Redis SETEX / Kafka producer 全部在同一 trace_id 下。
  - **验证脚本**：`/tmp/verify_server_spans.py` 注入指定 traceparent → 等 18s（tail_sampling `decision_wait: 10s` + batch flush）→ Jaeger `/api/traces/{traceID}` 查到 2 个 server span + 15 个 client/internal span。
  - 期间修了 1 个最隐蔽的坑（#24 otelcol tail_sampling 在 dev 把 99% 健康 span 采样掉）。

### 24. otelcol tail_sampling 在 dev 默默吃掉 99% SERVER span

- **症状**：Jaeger 里 api-registry / auth / admin 等服务只有少量 client span（INSERT/UPDATE/POST），**完全没有任何 SERVER span**。`POST /v1/apikey/verify` 这种关键入口 span 一条都看不到。
- **误诊路径**（花掉 ~1h）：
  1. 怀疑 `FastAPIInstrumentor().instrument()` 没 patch 到已实例化的 app → 加 `instrument_app(app)` 显式调用，仍无效。
  2. 写 introspection 脚本 import `admin.main`，确认 middleware stack 里确实有 `OpenTelemetryMiddleware`。
  3. 用 `TestClient + InMemorySpanExporter` 跑最小用例，能产出 SERVER span → 怀疑只在隔离环境生效。
  4. monkey-patch `OpenTelemetryMiddleware.__call__` 加 print，确认 uvicorn 实际请求**确实**进入这一层、`provider=TracerProvider`、`mw_tracer=ProxyTracer`（都是真实对象，不是 no-op）。
  5. 在 `tenant_middleware` 里 print `get_current_span().get_span_context()`，确认 `_Span` 实例、`is_valid=True`、`trace_id` 和入站 traceparent 完全一致。
  6. **关键转折**：span 确实被创建，但 Jaeger 里查不到这个 trace_id。直接调 `OTLPSpanExporter` 发一个手动 span，HTTP 200 但 Jaeger 还是 0。
  7. 发 ERROR 状态的 span → Jaeger 立刻出现。发 > 1s 的 slow span → 不出现。发 normal span → 不出现。
  8. **定位**：`scripts/otel/config.yaml` 配了 `tail_sampling`，策略是 `errors 全留 + slow (>1s) 全留 + 正常 1% probabilistic`。dev 联调时绝大多数请求都是 < 100ms 的 200 OK，命中 1% 概率被丢，所以 SERVER span 几乎全军覆没；ERROR span 因为匹配 `errors` 策略被保留；slow 单 span 在自己 trace 里没触发阈值也被丢。
- **根因**：tail_sampling 的策略对 dev 联调太激进，把"健康短请求"全部当噪音丢掉，但 dev 阶段恰恰就是要看这些。
- **修复**：`scripts/otel/config.yaml` 把 `tail_sampling.policies` 改成单一 `keep-all`（`sampling_percentage: 100`），注释里写明 prod 部署时要把 errors/slow + 1% 兜底接回来。改完 `docker compose restart otel-collector` 即生效（config 是 `volume mount` 不是 build-time）。
- **教训**：
  - "OTel SDK 创建 span" 和 "Jaeger 看到 span" 之间隔着 collector pipeline，中间任何采样/过滤都会让前端看不到。
  - 排查链路问题时，先发一个 **ERROR span** 探针 —— 如果它出现在 Jaeger 而 OK span 不出现，几乎可以锁定是采样策略。
  - dev 环境的采样应该 100%，prod 才上 tail_sampling。这俩用同一份 config 是错的，要么 env-aware，要么分文件。

---

---

## Phase 2 生产化收尾 优先级建议

> 截至 2026-07-10：Phase 2 端到端联调收尾，且「P0 技术债清偿 + kind 全量验证」（PR #8 / `031f588`）已顺手清掉原 Phase 2 生产化收尾 **全部 P0**，外加 P1 的两项 K8s 联调。下面是 Phase 2 生产化收尾（生产准备 + 灰度上线）的**剩余**待办，按上线风险排序。

**✅ 已清偿（PR #8 / `031f588` —— 原 P0 全部 + P1 两项）**：

| 项 | 实现 |
|---|---|
| 生产 K8s 自动化 `apihub_app` 业务账号（原 P0） | `deploy/k8s/base/shared/db-init/`：ConfigMap 内联 `00-roles`+`99-grants` + Job + Secret 模板 |
| CI 集成 smoke（原 P0） | `.github/workflows/smoke-auth.yml`：起 PG+Redis+Kafka → auth-svc → 真实 seed key verify |
| 锁定关键依赖版本（原 P0） | `apihub-core` pyproject：asyncpg 0.30 / aiokafka 0.12 / clickhouse-connect 0.7.7 / OTel 1.40.0 + 0.61b0 |
| trace-svc SQL 列名 #17（原 P0） | `trace-svc` SQL 对齐精简 CH schema（删 12 列、改 8 列名、`TIMEOUT`→`error_code`） |
| 失败重试链路 K8s 复现（原 P1） | `scripts/smoke/k8s-links.py` L3：retry-svc 真打 executor 死后端（latency ~2-4ms `backend_unreachable`，非 timeout） |
| admin dashboard 聚合 K8s 验证（原 P1） | `k8s-links.py` L4：dashboard 200、3 租户（同 namespace `apihub-system`） |

**P1（当前事实上的 P0）**：
- ~~workflow-svc 端到端联调~~ → **已验证（stub）**：`argo_mode=stub` 经 APISIX→dispatcher `/v1/jobs`→workflow-svc 端到端跑通（`k8s-workflow.py`：POST 201 + GET 200 running+steps），并修了 3 处潜伏 bug（`workflow_instance` 建表 `04-phase3.sql` / jsonb 双重编码 / `api_id`·`app_id`·`tenant_id` int→text）。真 Argo CRD 装集群验 `K8sArgoClient` 拆下轮。
- ~~dispatcher → executor → backend 的 traceparent 贯通**显式**验证~~ → **已验证**：`k8s-traceparent.py` 经 Jaeger API 断言同一条 trace 含 dispatcher SERVER span + executor `kafka.consume task-requests` span（trace `da5f3f94…`）；补 `_call_backend` 转发 W3C traceparent + executor OTel 初始化（原本 NoOp tracer 未导出 span）。
- ~~admin dashboard **跨 namespace** DNS~~ → **已验证（当前布局）**：数据层走 host compose（外部 `__HOST_IP__`），业务服务全在 `apihub-system`，服务间无跨 ns 数据调用；唯一真实跨 ns = APISIX(`apihub-ingress`)→dispatcher(`apihub-system`)，`k8s-links.py` L5 显式断言已绿。**待数据服务（PG/Redis/Kafka/CH/MinIO）迁入 `apihub-data` in-cluster 后需重验。**

**P2（短链路容错）**：
- CH 测试数据 INSERT 改成 `INSERT ... SELECT` 形式（CI 跑通就能加）
- `PG_SSL` 默认值从 `disable` 改成 `prefer`（dev 友好，prod 要求时再升）
- ClickHouse Kafka source 表的列默认值由生产端 JSON 带（避免依赖 MV COALESCE）—— 注：生产端 `ts` 已改发 CH 格式（`dispatcher/event.py:_now_ch_ts`），其余列仍建议显式带
- apihub-cli 加 `--dry-run` 模式（输出 diff 不入库，便于评审前预览）

**残留小项（非阻断，可并入 Phase 2 生产化收尾）**：
- retry `main.py` 硬编码 `executor_port=8003`（因 `EXECUTOR_SERVICE_TEMPLATE` 无 `{port}` 占位已无效，建议把端口也挪进 settings）
- `apihub-core` `test_kafka.py` 4 个预存失败（bytes-vs-str header，独立 tech debt，非本 PR 引入）

---

## K8s 联调结果（kind，2026-07-09）

> 在本机用 kind 真起集群，数据层复用 host docker-compose（PG/Redis/Kafka/CH/MinIO），把现有四条核心链路 + APISIX 网关 + trace 查 CH 在 K8s 跑通。分支 `feat/p0-debt-kind-validation`，16 commits（`5d2e6c9..d9a7350`）。

### Stage 0 · P0 技术债（全部清偿）

- **trace-svc SQL 对齐精简 CH schema**（commit `7eee445`）— 删 12 个不存在的列（`app_name/is_timeout/parent_trace_id/span_id/api_mode/env/gateway_node/biz_code/gateway_latency_ms/is_retry/retry_no/task_id`），改 8 个列名（`api_uuid→api_id`、`api_path→path`、`http_status→status_code`、`error_type→error_code` 等），`tenant_id` 改 String 透传，`TIMEOUT` 过滤改 `error_code LIKE '%timeout%'`（精简 schema 无 `is_timeout`）。25 单测全绿。
- **依赖锁定**（commit `d936363`）— `asyncpg==0.30.0` / `aiokafka==0.12.0` / `clickhouse-connect==0.7.7` / OTel `1.40.0` + instrumentation `0.61b0`。⚠️ **勘误**：计划/spec 原写 OTel api/sdk `1.36.0`，实测与 `0.61b0` 不兼容（OTel 固定 +offset 配对：`0.61b0 ↔ 1.40.0`），已改为 `1.40.0`。另补 `cramjam`（aiokafka 0.12 的 lz4 codec 底层依赖，`kafka.py` 用 `compression_type="lz4"` 但未声明）。
- **DB 账号 init Job**（commit `15d76c8`）— `deploy/k8s/base/shared/db-init/`（ConfigMap 内联 `00-roles.sql`+`99-grants.sql` + Job + Secret 模板），prod 托管 PG 的 `apihub_app` 业务账号自动化 provisioning。
- **CI smoke**（commits `33fd3c6`+`762fb5e`）— `.github/workflows/smoke-auth.yml` 起 PG+Redis+Kafka → auth-svc → 用真实 seed key `ak_test_a_demo001` verify。本地复现 green（`tenant_id=tenant_a`）。
- **勘误**：业务账号 `apihub_app` 的密码是 `apihub_app_dev_pwd`（`00-roles.sql:29`），**不是** `apihub_dev_pwd`（那是 superuser `apihub` 的密码）。本文档/计划多处写的 `apihub_dev_pwd` 用于服务连接是错的。

### Stage 1 · kind 集群（12 pods Running）

`scripts/kind/bootstrap.sh` 探测 host 网桥 IP、起 compose 数据层、建 kind 集群（预留 APISIX NodePort 30080）、构建并 load 11 服务镜像、apply kind overlay、等 ready。结果：**12 pods Running 0 restarts**（11 服务 + mock-backend），`api-registry /health/ready` 200。

### Stage 2 · 四链路（4/4 genuine green，commit `1468a04` + `ecd689a`）

`scripts/smoke/k8s-links.py`：L1 同步转发（dispatcher→mock-backend 200+echo）、L2 异步任务（Kafka→executor→`task` 表 succeeded）、L3 失败重试（`task-failures`→retry-svc→`retry_task` dead）、L4 admin 聚合（dashboard 200，3 租户）。L3 经修复后**真打** executor 死后端（latency ~2-4ms `backend_unreachable`，非 timeout）。

### Stage 3 · APISIX 网关 + trace 查 CH

- **APISIX 进数据面**（commit `92e15e9`）— helm 装 APISIX+etcd（NodePort 30080），consumer + key-auth（`X-API-Key`）+ route `/dispatch/*`→dispatcher。`curl -H 'X-API-Key: ak_test_a_demo001' http://127.0.0.1:30080/dispatch/smoke-sync/echo` → **200**（key-auth→dispatcher→mock-backend）；无 key / 错 key → 401。
- **trace 查 CH**（commit `d9a7350`）— trace-svc `/v1/trace/calls` 在真实 CH schema 上跑通，返回 7 行、列名正确（`api_id`/`http_status`/...），**无 `Unknown column` 报错** → Stage 0 的 trace-svc SQL 修复端到端验证通过。

### 联调暴露的 bug（Phase 2 生产化收尾 prep 价值最大）

**已修复（本分支）**：
1. **Dockerfile builder-stage bug（全部 11 服务）** — builder 以 root 跑 `pip install --user` → 落 `/root/.local`，runtime 却 COPY `/home/apihub/.local`（不存在）。修：builder 加 `useradd -m -u 1000 apihub` + `USER apihub`（commit `a5a4000`）。
2. **dispatcher 缺 `httpx[http2]`** — `main.py` 用 `http2=True` 但没 h2 包（commit `63d5dd1`）。
3. **workflow namespace 拼写** — `apihub-workflows` → `apihub-workflow`（Role/RoleBinding，commit `e5d7643`）。
4. **PG 连接风暴** — 单节点 kind 上 11 服务 × pool 50 打爆 PG。kind overlay 缩 `PG_POOL_MIN/MAX=2/10` + bootstrap `max_connections=500`（commit `e5d7643`+`26c5747`）。
5. **bootstrap Redis 端口同步 off-by-one** — 发布端口与写进 overlay 的 REDIS_PORT 不一致。修：bootstrap 用 `docker port` 回读实际发布端口再写 overlay（commit `ecd689a`）。
6. **retry→executor 走错端口** — retry worker 默认调 `executor:8003`，但 Service 只暴露 80 → 每次重试 30s timeout。修：kind overlay（`ecd689a`）**及 base retry configmap**（终审修复）均设 `EXECUTOR_SERVICE_TEMPLATE=http://executor.apihub-system/v1/internal/retry`（无 `{port}` → `.format` no-op → 走 Service:80）。⚠️ `main.py` 仍硬编码 `executor_port=8003`，因模板无 `{port}` 占位故已无效；Phase 2 生产化收尾建议把端口也挪进 settings 彻底干净。

**已识别、未修（列入后续）**：
- **CH Kafka-engine 摄取 —— 已修**（`dispatcher/event.py` `_now_ch_ts`）：根因不是 MV 列映射，而是生产者 `ts` 用 ISO-8601（`2026-07-09T09:58:41.537733+00:00`），CH `DateTime64` JSONEachRow 解析不了 → 整行被判解析错误、所有列落 default（epoch ts + 空字符串）。修：生产者改发 CH 格式 `YYYY-MM-DD HH:MM:SS.mmm`。**已验证**：直接往 Kafka 投 CH 格式 ts 的消息能正确入库（真实列），ISO 格式的落 default。dispatcher 镜像 rebuild 后即全链路通（ingest 级已证）。
- **PodSecurity prod 硬化 —— 已修**：11 个 base 服务 Deployment 补了 `securityContext.seccompProfile.type: RuntimeDefault`（prod `restricted` namespace 现在能过；kind overlay 仍 dev-only privileged）。mock-backend 在 kind overlay 内（dev），prod 不部署故无需。
- **apihub-core `test_kafka.py` 4 个预存失败** — `kafka.py:94-97` bytes-vs-str header 处理。非任何任务引入，独立 tech debt。
- **bitnami/kafka:\* 全系镜像已下架** → 用 `bitnamilegacy/kafka:3.7`（同 KRaft 布局）。
- **OTel 版本配对** — 见 Stage 0 勘误（`0.61b0 ↔ 1.40.0`）。

### 结论

- ✅ P0 四项全部清偿，单测 + 本地 smoke 验证。
- ✅ kind 集群 12 pods Running，四条核心链路 genuine green，APISIX 网关进数据面。
- ✅ trace-svc SQL 修复端到端验证通过（真实 CH schema）。
- ✅ CH 真实摄取链路根因已修（生产者 `ts` ISO→CH 格式，ingest 级验证通过）；dispatcher 镜像 rebuild 后全链路通。
- ✅ deferred 三项已全部清：CH 摄取、PodSecurity prod 硬化（11 Deployment 补 seccompProfile）、test_kafka 预存失败（stub decode bytes）。
