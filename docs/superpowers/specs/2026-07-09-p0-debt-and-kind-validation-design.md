# P0 技术债清偿 + 现有链路 K8s 验证（kind 全量）

- **日期**: 2026-07-09
- **状态**: Approved (设计评审通过，待 writing-plans)
- **范围**: 清掉 `docs/phase2-integration-findings.md` 列出的 4 项 P0 技术债；在本环境用 kind 真起集群，把现有四条核心链路 + APISIX 网关 + trace 查 CH 在 K8s 里跑通。
- **执行路径**: 路径 A —— 分阶段 + 复用 host docker-compose 数据层。

---

## 1. 背景与目标

APIHub 项目 Phase 2 端到端联调已收尾（tasks #95-#108 完成），但 `phase2-integration-findings.md` 末尾的 Phase 3 P0 清单仍是上线阻塞：

1. trace-svc SQL 用旧 schema 列名，查 ClickHouse 直接报错（已知 tech debt #17）。
2. 关键依赖（OTel / asyncpg / clickhouse-connect / aiokafka）未锁定版本，曾因 OTel 0.40 API 漂移咬过一次。
3. 生产 K8s 缺 `apihub_app` 业务账号的自动化创建（dev 是手动跑 SQL）。
4. 缺 CI smoke 回归，防"PG_USER 变 superuser / key hash 占位"类问题再发。

同时，所有验证至今停留在本地 docker-compose，K8s 部署清单（`deploy/k8s/overlays/dev`）从未在真实集群 apply 过。

**目标**：清掉上述 P0；用 kind 跑通「四条核心链路 + APISIX 网关进数据面 + trace 查 CH」，证明现有链路在 K8s 可用。

## 2. 非目标（Non-goals）

- 不做 Phase 3 功能（Portal / portal-bff / sdk-gen / 计费）。
- 不把数据层（PG/Redis/Kafka/CH/MinIO）做成 K8s manifests —— prod 是托管服务，放进 kind 既不 representative 又高风险。
- 不接"APISIX 回调 auth-svc 取 tenant_id 注入 X-Tenant-Id"完整 prod 网关流（用 APISIX 原生 key-auth 替代）。
- 不做压测 / 10w QPS 验证。
- workflow-svc 对真实 Argo Workflow CRD 的端到端验证（仍只跑单测 + stub 模式）。

## 3. 锁定决策

| # | 决策 | 理由 |
|---|---|---|
| D1 | 路径 A：分阶段，复用 compose 数据层 | 数据层进 kind 风险最高（Kafka/CH），且 prod 本就是托管服务 |
| D2 | trace-svc **trim 到精简 schema**，不 enrich | enrich 要改 Kafka source + MV + 生产者，爆炸半径超"还债"范畴；被砍字段作为 deferred feature |
| D3 | APISIX 用**原生 key-auth**，不接 auth-svc 回调 | 接 auth-svc 回调是另一量级工作；key-auth 已能证明"调用方经网关鉴权进 dispatcher" |
| D4 | 发现 Kafka→CH JSON drift 按**同类小债顺手修** | 与 trace-svc SQL 同源的老 schema 假设，不修则 trace 无数据 |
| D5 | Stage 3 失败**兜底到 Stage 2**，不回滚已交付成果 | Stage 0+1+2 已达成核心目标；Stage 3 是完整性加分 |

## 4. Stage 0 — P0 技术债

### 0a. trace-svc 对齐精简 CH schema

**真实 `api_call_log` 列**（`scripts/init-clickhouse/01-schema.sql`）：
```
ts, tenant_id(String), tenant_type, app_id(String), api_id(String), api_version_id,
trace_id, request_id, method, path, status_code, is_success, latency_ms,
request_size, response_size, error_code, error_msg, user_agent, client_ip(IPv4),
backend_type, backend_latency_ms, ai_model, ai_streaming,
token_prompt, token_completion, token_total, error_stack_ref
```

**列映射（code → 真实列）**：

| 现在（错） | 改成 | 处理 |
|---|---|---|
| `api_uuid` | `api_id` | rename |
| `api_path` / `api_method` / `api_version` | `path` / `method` / `api_version_id` | rename |
| `app_uuid` | `app_id` | rename |
| `caller_ip` | `client_ip` | rename |
| `http_status` | `status_code` | rename |
| `error_type` | `error_code` | rename |
| `req_id` / `req_size` / `resp_size` | `request_id` / `request_size` / `response_size` | rename |
| `ts_hour`（不存在） | `toStartOfHour(ts) AS hour` | 改派生 |
| `tenant_id` 强转 `int(...) else 0` | 直传 String | 修类型 |
| `app_name` / `is_timeout` / `parent_trace_id` / `span_id` / `api_mode` / `env` / `gateway_node` / `biz_code` / `gateway_latency_ms` / `is_retry` / `retry_no` / `task_id` | — | **从 SELECT 删除**；模型字段保留 `Optional=None`（API 契约向后兼容） |

**TIMEOUT 过滤**：无 `is_timeout` 列 → `error_code LIKE '%timeout%'` 近似（文档注明限制）。

**改动文件**：
- `services/services/trace/src/trace_svc/repository.py` — `_LIST_COLUMNS` / `_DETAIL_COLUMNS` / `_build_where` / `stats()`（含 `toStartOfHour`）全量重写。
- `services/services/trace/src/trace_svc/models.py` — 修 docstring（tenant_id/api_id/app_id 是 String 非 UInt64）；被砍字段保留 Optional。
- `services/services/trace/src/trace_svc/routes.py` — `_row_to_list_item` / `_row_to_detail` 的 `r.get("api_uuid")` 等键名全改；移除被砍字段的取值。
- `services/services/trace/tests/test_repository.py` + `test_routes.py` — 期望列名 / mock 行同步。

**出口标准**：`pytest services/services/trace/ -v` 全绿；`apihub_core` 测试不回归。

### 0b. 锁定关键依赖

`services/libs/apihub-core/pyproject.toml`：4 类库从 `>=` 改精确 pin（其余保持 `>=`）：
- `asyncpg==0.30.0`（本环境已验证）
- `aiokafka==0.12.0`
- `clickhouse-connect==0.7.7`
- OTel 一致集：`opentelemetry-api==1.36.0` / `opentelemetry-sdk==1.36.0` / `opentelemetry-exporter-otlp==1.36.0` / `opentelemetry-instrumentation-fastapi/httpx/asyncpg/redis==0.61b0`

精确 patch 用 `uv pip compile` 校验可解析；某 patch 不可用则降一档 pin 并在此记录。

**出口标准**：`uv pip install -e services/libs/apihub-core` 成功 + apihub-core 测试全绿。

### 0c. K8s DB 账号 init 自动化

新增 `deploy/k8s/base/shared/db-init/`：
- `configmap.yaml` — 内联 `scripts/init-db/00-roles.sql` + `99-grants.sql`（单一事实源，与 dev 一致）。
- `job.yaml` — `postgres:16-alpine`，`psql "$PG_SUPER_URL"` 执行 ConfigMap 里的 SQL，`restartPolicy: OnFailure`。
- `secret.example.yaml` — 超管密码 Secret 模板（注明 prod 走 SealedSecret/ExternalSecret）。

Kind 运行时 compose 已挂同样 SQL 初始化，`apihub_app` 角色已存在；此 Job 作为 prod 托管 PG 的通用 provisioning 入口交付物。

**出口标准**：Job manifest `kustomize build` 渲染合法；SQL 与 dev 一致（diff 仅包装层）。

### 0d. CI smoke 回归

新增 `.github/workflows/smoke-auth.yml`：
1. `docker compose -f docker-compose.dev.yml up -d postgres redis`（起最小栈）。
2. `uv` 用 python3.11 装 `apihub-core` + `auth`。
3. 起 `auth-svc`（uvicorn，端口 8002）。
4. `curl -X POST /v1/apikey/verify -H 'X-API-Key: ak_test_a_demo001'` → 断言 200 + 响应含 tenant 上下文。
5. teardown。

本环境用 docker + python3.11 手动复现该 workflow 全步骤，证明它能跑通。

**出口标准**：workflow 文件合法；手动复现 4 步全过。

## 5. Stage 1 — kind 起服务

### 工具安装（~/.local/bin，无 sudo）
`kind`、`kubectl`、`kustomize`（指定版本，下载二进制）。

### kind 集群
- 集群 config 预留 `extraPortMappings`：一个端口映射给 Stage 3 的 APISIX gateway NodePort。
- 网络：kind pod 通过 **host docker 网桥网关 IP**（动态探测，通常 `172.17.0.1`）访问 host 暴露端口。
- **Kafka advertised-listener override**：改 compose 的 `KAFKA_CFG_ADVERTISED_LISTENERS`，EXTERNAL 改成 `<host-bridge-ip>:9094`，否则 pod 连上后被 advertize 的 `localhost` 坑死。

### 新增 `deploy/k8s/overlays/kind/`
1. **共享 ConfigMap `apihub-shared-infra`**：`PG_HOST`/`REDIS_HOST`/`CH_HOST`=<host-ip>、`KAFKA_BROKERS`=<host-ip>:9094、OTel endpoint=<host-ip>:4317、APISIX 字段 Stage 3 填。
2. **共享 Secret `apihub-shared-secret`**：`PG_PASSWORD=apihub_dev_pwd` 等（与 compose 一致）。修复"Secret 空导致 Settings() 崩"缺口。
3. 每个 Deployment 的 `envFrom` **追加** `apihub-shared-infra` + `apihub-shared-secret`（追加在末尾 → 覆盖 base configmap 的 prod DNS 值）。
4. 副本压到 1、资源请求降到 kind 可承受。
5. **mock-backend**（python http.server 或 httpbin）Deployment + Service，作为 L1 同步转发后端目标。
6. dispatcher 仍是 Deployment（dev overlay 已用 deployment.yaml，rollout.yaml 不在 overlay resources）→ **无需 argo-rollouts controller**。

### 镜像构建 + load
`make docker-build SERVICE=X`（context=仓库根，Dockerfile 自包含多阶段，自带 apihub-core）→ 产出 `registry.apihub.internal/apihub/X:0.1.0-dev` → 11 服务循环 `kind load docker-image`。镜像名与 deployment `image:` 完全一致 + `imagePullPolicy: IfNotPresent` → kind containerd 命中本地，不外拉。

### 起栈 + 健康
```
kustomize build deploy/k8s/overlays/kind | kubectl apply -f -
kubectl wait --for=condition=ready pods -n apihub-system --all --timeout=300s
```

**出口标准**：全 pod Ready；port-forward 任一服务 `/health/ready` 返回 200。

## 6. Stage 2 — 四链路直打服务（port-forward）

`scripts/smoke/k8s-links.py`（python3.11，真实调用，无 stub）：

| 链路 | 驱动 | 断言 |
|---|---|---|
| L1 同步转发 | seed api 指向 mock-backend，POST 经 dispatcher | 200 + backend 收到请求 |
| L2 异步任务 | 投 Kafka `task-requests`（backend=mock） | executor 消费 → PG `task_instance` 出现 succeeded |
| L3 失败重试 | 投 `task-failures` 死地址 | retry 建 retry_task → 调 executor `/v1/internal/retry` → 达 max_attempts `status=dead` + retry_attempt N 行 |
| L4 admin 聚合 | GET admin-bff `/v1/admin/dashboard` | 返回 tenant totals，与 compose seed 一致 |

每条独立断言，失败即停并打印 `kubectl describe/logs` 定位。

**出口标准**：四链路全绿。= 「现有链路在 K8s 跑通」核心目标达成。

## 7. Stage 3 — APISIX 进数据面 + trace 查 CH

### 3a. APISIX + etcd（helm）
- 装 helm → `helm repo add apisix` → install 到 `apihub-ingress`，override `apisix-values.yaml`：`gateway.type` LoadBalancer→NodePort（去阿里云 SLB annotations）；`admin_key` 固定；`dashboard.enabled: true`；etcd 随 chart 单节点。
- Admin API 配置：consumer + `key-auth` 插件（seed key）；route 匹配测试 URI → upstream `dispatcher.apihub-system:80`，挂 key-auth。
- **出口标准**：`curl -H 'X-API-Key: <seed>' http://localhost:<nodeport>/<route>` → 经 key-auth → dispatcher → mock-backend → **200**。

### 3b. trace 查 CH（端到端验证 0a）
- 先校验 Kafka→CH 管道：dispatcher `event.py` 投出的 JSON key 对齐 `api_call_events_src` 列；有 drift 则按 D4 顺手修。
- 产生若干调用（经 3a，或退用 Stage 2 直打）→ 等 CH Kafka engine 消费 + MV 转存（~数秒）→ `GET trace-svc /v1/trace/calls`（port-forward）→ 断言 **≥1 行 + 列名正确**（0a 重写后的 SELECT）。
- **出口标准**：trace 查出真实行，列名正确。= Stage 0a 修复的最终兜底证明。

### 风险与兜底
| 风险 | 概率 | 兜底（D5） |
|---|---|---|
| APISIX/etcd 在 kind 起不来 / NodePort 不通 | 中 | 不影响 trace 验证；停在此处，Stage 2 已达标 |
| Kafka→CH JSON drift / 摄取延迟 | 中高 | 修 producer/source 对齐；仍不通则 trace 降级为 `INSERT ... SELECT` 造一行验证查询本身 |
| trace-svc 修完仍查不出 | 低 | 0a 单测已覆盖；再炸即补单测 |

## 8. 交付物清单

| 类别 | 产出 |
|---|---|
| 代码修复 | trace-svc（repository/models/routes/tests）；apihub-core pyproject 依赖锁 |
| K8s 新增 | `base/shared/db-init/`；`overlays/kind/`（infra 重定向 + 共享 secret + mock-backend + envFrom patch） |
| CI | `.github/workflows/smoke-auth.yml` |
| 脚本 | `scripts/smoke/k8s-links.py`；Stage 3 APISIX+trace 校验脚本；kind 引导脚本（装工具/起集群/改 Kafka advertize/build/load/apply） |
| 文档 | 本 spec；`phase2-integration-findings.md` 追加"K8s 联调结果"小节 |

## 9. 验证总策略

- **Stage 0**：`pytest`（trace-svc 全绿 + apihub-core 不回归）+ CI smoke 本地复现。
- **Stage 1–3**：kind 集群实跑 + smoke 脚本断言；每阶段出口标准明确，撞墙即停于最近成功阶段并记录于 findings。

## 10. Deferred / 后续

- trace UI 字段补全（gateway/backend 延迟、retry、timeout 标志）→ enrich schema + 生产者 + MV（独立 feature）。
- APISIX 回调 auth-svc 注入 X-Tenant-Id 的完整 prod 网关流。
- workflow-svc 对真实 Argo CRD 端到端验证。
- 跨 namespace DNS、prod 账号 init 在真 prod 集群的复现。
