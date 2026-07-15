# Phase 4 全模块代码 vs 设计审计 —— 发现清单

时间：2026-07-15
范围：APIHub 全部 15 个微服务 + `apihub-core` 共享库 + Go quota，对照 `docs/` 13 份设计文档逐模块核对。
方法：codegraph 索引（377 文件 / 3836 节点）+ 5 组并行审计 agent，逐服务比对设计契约（端点/职责/依赖）与代码实现，重点戳穿"有 commit 有文档但代码/部署是空壳"的部分。

> 与 [phase2-integration-findings.md](phase2-integration-findings.md) 配套。后者记录 Phase 1-2 端到端联调修复的 24 个坑；本文聚焦 phase2 之后（尤其 Phase 4）的完成度与系统性裂缝。

---

## 0. 一句话结论

**Phase 1-2 骨架真实可用，phase2 文档记录的 24 个历史坑基本都真修了；但 Phase 4 是典型的"单服务代码写了、接缝处全没接上"——4 个新服务生产环境部署不出去，自动重试链路在生产永不触发，多 Region 双活的核心路由是空的。** 代码整体不像"乱写的烂摊子"，而是"功能堆得快、集成与验收没跟上"。

> **架构层（design-level）判断见 §9**——其中"服务边界泄漏"与"Kafka 无契约"是下面多个 P0/P1 bug 的架构根因，只补实现不修架构，同类问题会持续长出来。

---

## 1. 总体健康度矩阵

| 服务/模块 | 状态 | 完成度 | 一句话 |
|---|---|---|---|
| **apihub-core** | ⚠️ 有缺陷 | 85% | 地基可靠，但 `admin_db_session` 不写审计 + RLS 变量 f-string 拼接 |
| **dispatcher** | ⚠️ 有缺陷 | 85% | 数据面/流式/事件全真，trace_id 对不上 Jaeger + 路径变量 stub |
| **quota (Python)** | ✅ 健康 | 100%+ | Lua 原子三层限流，超出设计 |
| **api-registry** | ⚠️ 有缺陷 | 60% | 生命周期+工单扎实，APISIX 下发纯 stub + 版本管理零实现 |
| **auth** | ⚠️ 部分 | 60% | JWT/APIKey 真实，HMAC/OAuth2 全空 + App CRUD 缺 |
| **executor** | ⚠️ 有缺陷 | 80% | 状态机/幂等/超时齐，但失败不投 `task-failures` |
| **retry** | ⚠️ 有缺陷 | 机制全但入口断 | 自动+手动入口都断，生产空转 |
| **workflow** | ✅ 健康 | 95% | stub+真 Argo 双实现，四服务最扎实 |
| **trace** | 🔧 部分 | 65% | 核心查询 OK，export 501/compare 缺/MinIO 堆栈没做 |
| **admin** | ⚠️ 有缺陷 | 80% | 审计+GDPR归档真做，RBAC 二元 + CSV 501 |
| **portal** | ⚠️ 轻微 | 85% | 应用层最健康，GDPR 转发 4 端点全 404 |
| **docs** | 🔧 部分 | 65% | OpenAPI/curl/Py/JS 真，参数表 `return []` + Java/HTML 缺 |
| **billing** | 🔧 部分 | 60% | 出账逻辑真，定价硬编码 + 无对账 + 未部署 |
| **ai-gateway** | ❌ 孤岛 | 代码70%/集成0% | 代码能跑但无人调用/未部署/未鉴权/token计费没接线 |
| **notification** | ❌ 空壳 | 20% | 设计 5 渠道只做 Webhook，连 `/internal/notify/send` 都没 |
| **Go quota** | ❌ 悬空 | 假兼容 | 响应字段不兼容 + 算法退化 + 未部署，不能替代 Python |
| 多 Region 双活 | 🔧 PPT 重 | ~50% | quota region 前缀真做，写亲和/复制脚本全坏 |
| GDPR 合规 | ⚠️ 85% | — | PII/anonymize/export 真，withdraw 是文档谎言 |
| 高级分析 | ⚠️ | — | funnel/co-occurrence 真查询，零测试 |

图例：✅ 完成 健康 / ⚠️ 完成但有缺陷 / 🔧 部分完成 / ❌ 空壳或孤岛

---

## 2. P0 —— 核心链路生产不可用 / 严重安全合规缺口

### 2.1 自动重试链路在生产永不触发（最严重）

- **症状**：retry-svc 完整实现了消费→指数退避→Redis ZSet 延迟队列→重投 executor→死信，但生产环境 retry_task 永远是 0。
- **根因**：全代码库没有任何服务向 Kafka topic `task-failures` 投递消息。
  - executor 失败时只 emit 可观测用的 `task-status`（`services/services/executor/src/executor/processor.py:89`）。
  - dispatcher 异步任务投的是 `task-requests`（`services/services/dispatcher/src/dispatcher/task_dispatcher.py:50`）。
  - retry 消费的是 `task-failures`（`services/services/retry/src/retry/consumer.py:28`）。
  → 生产者与消费者 topic 对不上，**整个失败重试/死信机制是死代码**，所有 backend 失败被静默吞掉。
- **为何 phase2 没发现**：phase2 文档"task #99 重试链路验证通过"是因为 smoke 脚本 `scripts/smoke/k8s-links.py:58` **手动直投** `task-failures`，绕过了真实生产者，掩盖了入口缺失。这是 phase2 验证的系统性盲区（见 §6）。
- **修复**：executor 失败分支（`processor.py:80-85`）追加 `kafka.emit("task-failures", …)`；或让 retry 改消费 `task-status` 并过滤失败状态。同时给 retry_task 加幂等唯一约束（见 2.7），否则 at-least-once 重投会建重复任务。

### 2.2 retry 手动重试静默失效

- **症状**：运营在后台点"重试"（`POST /v1/retry/{id}/trigger`）后任务毫无反应、也不报错。
- **根因**：`retry/routes.py:81-96` 的 trigger 只调 `repository.requeue_for_retry`，而该函数（`retry/repository.py:207-228`）只做 SQL UPDATE（status=pending + next_retry_at=NOW()），**没有调 `delay_queue.schedule`**。worker 只轮询 Redis ZSet（`retry/worker.py:90-103`），永不扫 PG。→ 被手动复活的死信任务永远停在 pending。
- **修复**：trigger 成功 UPDATE 后补 `await delay_queue.schedule(task_id, delay_ms=0)`。

### 2.3 Phase 4 四个新服务生产部署不出去

- **症状**：git log 有"Phase 4 API 市场化（billing）/ AI 网关 / notification"等 commit，但生产环境压根起不来。
- **根因**（部署集成缺口）：
  - `ai-gateway`、`billing`：`deploy/k8s/services/` 下**无对应服务目录**，无 Deployment manifest。
  - `notification`：**无 Dockerfile**（`make docker-build SERVICE=notification` 直接 fail），其 Deployment 引用的镜像 `notification:0.1.0-dev` 永远没镜像可拉。
  - `portal`、`notification`、`ai-gateway`、`billing`：**不在任何 dev/staging/prod/prod-bj overlay**（portal 仅在 kind overlay）。
  → "Phase 4 上线"在 k8s 层面是空话。
- **修复**：为四服务补 Dockerfile + base Deployment + 各 overlay 引用；notification 的 Dockerfile 是前置阻塞项。

### 2.4 `admin_db_session` 不写审计（等保三级合规缺口）

- **症状/根因**：`services/libs/apihub-core/src/apihub_core/db.py:125` docstring 声称"每次调用都会写 audit_events（外部可观测）"，CLAUDE.md 也明文规定"admin_db_session() bypasses RLS … and writes audit events"。但函数体（`db.py:130-139`）只 `SET LOCAL app.is_platform_admin='true'` 后 yield，**零审计写入**。executor/quota/ai-gateway/admin/retry 多个服务日常调用它绕过 RLS，**整个 RLS 旁路处于无追溯盲区**。
- **影响**：直接违反等保 2.0 三级"关键操作可审计"要求；设计文档里"跨租户访问 → 审计 + 告警"的安全模型出现缺口。
- **修复**：yield 前后插入 audit_log 写入，或强制传入操作描述参数。

### 2.5 RLS 会话变量 f-string 拼接（SQL 注入面）

- **症状/根因**：`db.py:105-106` `f"SET LOCAL app.tenant_id = '{ctx.tenant_id}'"`。`SET` 不支持 `$1` 参数化，但正确写法是 `SELECT set_config('app.tenant_id', $1, true)`（第三参 `true`=local）。叠加 `config.py:79` 的 `jwt_secret="dev-only-insecure-secret"` 不安全默认值且**无 prod 启动断言**——prod 漏配密钥即可伪造 JWT，注入含 `'` 的 tenant_id 改写 RLS 上下文。
- **修复**：改用 `set_config($1, $2, true)`；config 密钥（`jwt_secret`/`pii_encryption_key`/`oss_secret_key`）加启动期断言，prod 拒绝默认值。

### 2.6 （关联）CronJob 漏挂 secret 卷，定时任务运行即失败

- **症状/根因**：`deploy/k8s/base/shared/audit-archive-cronjob.yaml:30` 与 `data-cleanup-cronjob.yaml:30` 都 `cat /etc/apihub/api-key`，但 Job template **未定义 `volumes:` + `volumeMounts:`**（也无 `envFrom`）。运行时 `cat` 报 "No such file"，curl 发不出 `X-API-Key`，audit 归档与数据清理两个定时任务全部失败。
- **影响**：audit 归档代码本身是 Phase 4 最扎实的交付（见 §5），但被这个 10 行 yaml bug 卡死在运行期。
- **修复**：两个 CronJob template 补 secret 卷挂载。

### 2.7 retry 无幂等唯一约束（前置 P0 的衍生）

- **症状/根因**：`scripts/init-db/03-phase2.sql:75-82` 对 retry_task 只建非唯一索引，`retry/repository.py:42-46` 注释自承"假设 Kafka 投递不会重投同一条"。一旦 §2.1 修复、`task-failures` 真有生产者，Kafka at-least-once 重投会创建重复 retry_task。
- **修复**：retry_task 加 `(trace_id, attempt)` 或业务唯一键的唯一约束 + ON CONFLICT。

---

## 3. P1 —— 设计契约关键功能是 stub 或假实现

### 3.1 api-registry 的 APISIX 路由下发是纯 stub

- **证据**：`services/services/api-registry/src/api_registry/routes.py:147-149`（publish）与 `:219`（retire）都是注释掉的 `from api_registry.apisix_client import publish_route` + `TODO`；仓库内不存在 `apisix_client.py`（grep 零命中）。
- **影响**：发布/下线**只改库状态**，APISIX 数据面不感知，"发布即可调、下线即 410"的闭环没接通。真正把流量导入 dispatcher 的是 phase2 kind 联调里手动 helm 装的静态 route，而非 api-registry 自动下发。这是"接口注册中心"的核心职责之一，目前只做了一半（元数据管理 ✅，路由生效 ❌）。

### 3.2 api-registry 版本管理两接口完全缺失

- 设计 §3.1 的 9 个 admin API，实现缺 3 个：`PUT /v1/apis/{id}`（更新）、`GET .../versions`（版本列表）、`POST .../rollback`（回滚）。代码里**没有任何"版本快照"概念**，rollback 即使补端点也需先设计归档表。另：`list_apis` 无过滤参数（设计写"支持过滤"）；钉钉审批（`change_request.py:290`）是 stub（返伪造 ID，无真实审批单/回调）。

### 3.3 notification 近乎空壳

- 设计 §3.15 要求钉钉/邮件/短信/Webhook/站内信 5 渠道，实际**只实现 Webhook（1/5）**：
  - `/internal/notify/send`、`/internal/notify/batch` 端点**不存在**（`routes.py:26-63` 仅 webhooks CRUD + `/test` + `/health`）——业务服务根本无法请求发通知。
  - `scripts/init-db/06-notification.sql` 只 1 张 `webhook_subscription` 表（无 notification_log/template/channel）。
  - 钉钉审批集成（发布/授权→审批流→回调→发布）零代码。
  - `Channel` 抽象接口、模板化、限流、审计全无；重试 3 次固定间隔（设计要求 5 次指数退避）。
- commit `c7ea229` 老实命名"Webhook 通知"，但 `docs/03-services.md §3.15` 仍按 5 渠道描述，文档严重超前。

### 3.4 ai-gateway 是孤岛

- 代码本身真实（Provider 插件化 ABC + 注册、OpenAI 兼容非流式、SSE 流式 `routes.py:71-84`、Provider Key AES-256-GCM 加密、模型路由 `ai_model_route` 表）。但：
  - **无任何服务调用 ai-gateway**——dispatcher 自己做 AI 流式转发与 token 解析（`dispatcher/forwarder.py:114-248`），ai-gateway 是一套未使用的平行实现。
  - 未部署 k8s（无 Deployment 目录）。
  - `/v1/chat/completions` 放进 `skip_auth_paths`（`main.py:20`），未鉴权。
  - commit 宣称的"Token 计费"在 ai-gateway 内零接线（不 emit Kafka / 不扣 quota / 不落库；usage 只回传 SSE 客户端）。
- **修复方向**：要么让 dispatcher 的 AI 流式改走 ai-gateway（统一计费/限流/多 Provider），要么明确 ai-gateway 定位并补部署。

### 3.5 `withdraw_consent` 是文档谎言（GDPR）

- **证据**：`services/services/auth/src/auth/identity.py:217-231` 函数 docstring 与路由 docstring 都写"撤回全部同意 → 触发账号匿名化"，但代码只 `UPDATE user_consent SET status='withdrawn'`，**从不调用 `anonymize_user`**。GDPR"撤回=删除"的默认契约是假的。测试 `test_withdraw_ok` 只断言路由转发了 user_id，没断言匿名化发生。

### 3.6 Portal /privacy 页面 4 个 GDPR 端点全 404

- **证据**：`services/services/portal/src/portal/routes.py:68-134` 的 4 个 GDPR 端点（account DELETE / account/export GET / consent GET / consent/withdraw POST）转发到 `f"{auth_base}/auth/account"`，缺 `/v1` 前缀；而同文件 `_forward("POST", "/v1/auth/register")` 是对的。`auth_base` 剥了 `/v1/apikey/verify`，实际打到 `http://auth.apihub-system/auth/account` → auth 返回 404。前端 `Privacy.tsx` 调对了 `/v1/portal/auth/...`，BFF 二次转发断链。commit `cdc4432` 修了 `Request` import 但没发现此 bug。

### 3.7 多 Region 写亲和不工作

- ADR-013（租户亲和全双活）核心路由是空的：
  - APISIX `tenant-affinity` Lua 插件（`deploy/apisix/plugins/tenant-affinity.lua`）读 `ctx.consumer.home_region`，但**全仓库无任何 APISIX Consumer 资源/初始化脚本给它注入 home_region**（grep 只在 apisix-values.yaml 挂了插件名）→ 插件第 26 行直接 `return`，等于没装。
  - `/internal/auth/check`（`auth/routes.py:85`，带 home_region）**无任何调用者**，是死代码（插件本应调它，实际走 consumer 字段）。
  - MirrorMaker 脚本坏：`scripts/multi-region/deploy-mirrormaker.sh:33/57` 用未定义变量 `$SH2BJ_TOPICS` / `$BIDIR_TOPICS`（只定义了 `TOPICS`），whitelist 为空直接退出。
  - PG 逻辑订阅名实不符：`setup-pg-logical-replication.sh` 注释/变量名说 per-tenant，实际 `CREATE PUBLICATION ... FOR ALL TABLES` 订阅整库，且双向只建了 1 个方向。
  - "CH 双集群跨 Region 查询"只有 commit message + 一个 `PEER_REGION_CH_HOST` env 占位，trace-svc 无代码读它。
- **真做了的**：Go quota 的 region 前缀 + `QUOTA_REGION_SPLIT_RATIO`（`services/go/quota/internal/limiter/redis.go`）；`tenant.home_region` 列（`08-tenant-home-region.sql`）；prod-bj overlay/terraform；Thanos receive + 多 Region 告警规则。但 prod-bj overlay 只部 11 个老服务，Phase 4 新服务"全双活"不适用。

### 3.8 Go quota"API 兼容"是假兼容且未部署

- **声称**：commit `1441bda`"Go 重写 quota（精简版，5 端点，保持 API 兼容）"。
- **实际不兼容**：
  - 端点：Go 5 个（check/check-strict/refund/usage/health），Python 7 个——Go **缺 billing、plans**。
  - 响应字段：Go `QuotaCheckResponse` 给 `reset_ms`+`current`，Python 给 `retry_after_seconds`；`UsageResponse` Go 是 `{points:[...]}` 列表，Python 是扁平 `{second,minute,day}` 对象——形状完全不同。
  - Redis key：Go `t:{region}:rate:{tenant}:{api}:{app}:{slot}`，Python `t:{tenant}:rate:{api}:{app}:s|m|d:{slot}`——多了 region 段且层级不同，**两端计数器不共享状态，切换即清零**。
- **算法退化**：Go 每 tier 一次独立 INCR（3 次 RTT，跨 tier 非原子），比 Python 单次原子 Lua（`quota/lua_scripts.py:CHECK_AND_INCR`）多竞态窗口且延迟更高——与"Python 性能最敏感服务改 Go 提性能"的初衷相悖。另有 bug：`handler/quota.go:54` `if resp.RuleSource == "api"` 永假（limiter 初始置 "default"）。
- **未部署**：`deploy/k8s/services/quota/deployment.yaml` 镜像 = Python quota；`Makefile:83-85` docker-build 用 `services/services/quota/Dockerfile`；Go 的 `services/go/quota/Dockerfile` 无任何引用；且 Go 版只暴露 `/v1/quota/health` 无 `/health/ready`，若部署会被 startupProbe（`deployment.yaml:68`）卡死。
- **结论：当前状态 Go quota 不能替代 Python quota。** 要替代需先补端点、对齐响应字段与 Usage schema、limiter 改回 Lua 原子、接 apihub_core 鉴权/RLS、修 health、切镜像。

### 3.9 dispatcher 调用事件 trace_id 与 Jaeger 对不上

- **证据**：`dispatcher/forwarder.py:284/309/347` 三个 `_emit_*` 都传 `trace_id=request.headers.get("X-Trace-Id", "")`，`event.py:55` 对空值 fallback 随机串 `_gen_trace_id()`。结果 ClickHouse 里 `trace_id` 多为 `trc_xxx` 随机值，**无法 join Jaeger**——trace-svc"单次调用详情含 span"（设计 §3.8）的核心能力失效。Kafka header 里倒有真 traceparent，但 CH 摄取读 payload value 不读 header。
- **修复**：改用 OTel span context（`trace.get_current_span().get_span_context().trace_id`，`routes.py:38-44` 的 `_trace_id()` 已示范）。

### 3.10 auth 的 HMAC 与 OAuth2 完全未实现

- 设计 §3.5 列为一级鉴权方式，全仓库零实现（hmac 仅出现在 `apihub_core/oss.py` 的 OSS 签名；无任何 oauth 引用）。JWT/APIKey 真实（sha256 比对 + 正负缓存 + expires_at 校验）。另：**App CRUD 缺失**（只有 Key CRUD，app 只能靠 DB seed）；Key 轮换缺 `last_rotated_at`（ADR-006 要求），外部 Key 无 1 年默认过期；`/internal/auth/check` 未加入 `skip_auth_paths`（与 `/v1/apikey/verify` 一致性 bug，APISIX 无 X-API-Key 调用会被拦死）。

---

## 4. P2 —— 缺失或降级（非阻塞但欠账）

- **trace**：`export` 501 stub（`routes.py:88`）；`compare` 端点完全缺失（设计 §3.8 五 API 之一）；MinIO 错误堆栈（按 trace_id 索引）未实现（grep 零命中）；CH 不可用时吞异常返回空（`repository.py:103` 等，可观测性盲点）；`main.py:29` 模块名字符串写成 `trace.main`（裸跑坏，Makefile 用 `trace_svc.main` 不受影响）。
- **docs**：参数表 `_build_parameters` `return []`（`openapi_gen.py:100`，OpenAPI parameters 字段不生成）；Java 示例缺（设计列 4 语言，实现 3）；HTML 文档片段端点 `GET /apis/{id}/docs` 缺；在线 try 已迁至 portal（分工可接受）。
- **admin**：RBAC 只是二元超管判断（`routes.py:44`，无 editor/viewer 角色粒度，设计 §3.11"基于 RBAC"被夸大）；CSV 导出 501 stub（`routes.py:123`）。
- **billing**：定价硬编码 `price_calls=5; price_tokens=10`（`billing_job.py:78`，无阶梯/按 API 差异化）；配额换算 `calls_per_day*30` 粗糙；无对账（直读 CH，不与 quota Redis 交叉校验）；无 CronJob（`/v1/billing/periodic` 只能手动触发）。
- **apihub-core**：`/health/ready` 假探针（恒 ready，`middleware.py:116`，DB 宕机仍报 Ready）；Kafka 消费侧不自动还原 TenantContext（`kafka.py:177`，executor/retry 各自手写易漏）；redis `_prefix` 无 ctx 时 fail-open（`redis.py:34`）；auth `required_scopes` 死参（`auth.py:22`）；clickhouse.py 注释引用不存在的 `ch_tenant_filter()`（实际是 `current_tenant_id_or_none()`）；CH 无 RLS 且 `ch_session` 不强制 tenant 过滤。
- **dispatcher**：参数校验缺失（设计 §3.2"参数校验"，request body 不对 request_schema 校验）；`_render_url` 路径变量替换是 stub（`forwarder.py:180`，`resolver._extract_path_params` 从未被调用，带路径变量的 backend_url 会 404）；写 PG task 表违反"Redis-only"契约（at-least-once 合理但契约未同步）；死代码 `_b`（`forwarder.py:352`，phase2 #14 遗留）。
- **executor**：`/v1/internal/retry` 不传 W3C traceparent（重试 span 断链）；`reset_stale_running` 潜在 UnboundLocalError（`repository.py:94`）；Webhook 回调（设计步骤 4，标"可选"）未实现。
- **portal**：`subscribe_plan` 首次无 subscription 行时静默 no-op（`repository.py:370`，应 upsert）；Webhook 代理未透传 `X-API-Key`/`X-Tenant-Id`（`routes.py:199`，生产会 401）；`get_billing_summary` 失败静默返零（`repository.py:337`）。
- **sdk-gen**：设计第 10 个服务，代码完全不存在（roadmap 排 Phase 3，当前未启动）。

---

## 5. 真实交付、值得肯定的部分

- **Python quota**：Lua 原子三层限流（`lua_scripts.py:CHECK_AND_INCR`，一次 RTT 内 INCRBY+EXPIRE 三 tier，无跨 tier 竞态），**超出设计文档的 INCR+EXPIRE**；check/check-strict/refund/usage/billing/plans 端点真做；Redis 故障 fail-open + 告警。
- **workflow**：stub（`StubArgoClient`）+ 真 Argo CRD（`K8sArgoClient`）双实现，6 个 API 全真，cancel（`shutdown=Stop`）/resume（走 argo-server PUT）/logs（核心 v1 pods/log 按 annotation 过滤 step）全落地，phase 映射齐全。四服务最扎实。
- **api-registry 生命周期 + change_request 工单**：状态机（draft→published→deprecated→retired，SQL WHERE 守卫 + 409）完整；工单 7 端点 + 分级审批状态机（dev 自助 / staging-prod 走 pending，approve/reject 强制超管）；声明式 YAML apply 走工单链路（phase2 #18/#19/#20 验证）。
- **dispatcher 数据面**：httpx 连接池（max_connections=500/http2）、AI SSE chunk 流式、Kafka 事件（CH 格式 ts `_now_ch_ts`，phase2 ISO→CH 修复）、W3C traceparent 透传、visibility 三级授权、masking 日志脱敏。
- **GDPR 核心**：AES-256-GCM PII 加密（`apihub_core/pii.py`）、anonymize（清 Redis refresh/verify + 吊销 Key）、export（聚合账户/租户/应用/Key/账单 5 表）、`user_consent` 表（`09-consent.sql`）、6 个端点；测试扎实（test_identity 11/test_pii 8）。
- **audit 归档**：批量读→按 (tenant,yyyy,mm) 分组→gzip JSONL→`oss.put_object`（真 AWS V4 签名）→**成功才 DELETE**（`admin/repository.py:303`），含 `test_upload_failure_skips_delete`/`test_multi_batch`/`test_multiple_tenants` 等安全用例 + CronJob——**Phase 4 横切里最实在的一块**（仅被 §2.6 的 secret 卷 bug 卡在运行期）。
- **高级分析**：funnel（`groupArray` 按 trace_id 聚合调用序列）+ co-occurrence（CH 自连接）真查询（`trace_svc/repository.py:286`），前端 Analytics.tsx 真消费（虽零测试）。
- **测试基础设施整体健康**：`test_kafka` 4 个历史失败已修（phase2 #8）；dev 栈依赖面小（仅 PG-RLS + identity 需真 PG，module-level skip 容忍）；薄弱点集中在 Phase 4 新代码回归（trace 分析/portal GDPR/withdraw 链零测试）。
- **数据层**：`init-db` 00-09 + `init-clickhouse`/`init-kafka`/`init-minio` 表/桶齐全（plan/subscription/billing_record/ai_provider/ai_model_route/home_region/user_consent 全有），**不是空壳**。空壳集中在部署集成与跨组件接线。

---

## 6. phase2 验证的系统性盲区（方法论教训）

phase2 文档"四链路 genuine green"存在一个共性盲区：**所有链路都靠 smoke 脚本直投 Kafka topic 绕过真实生产者**，导致 §2.1 这种"生产者与消费者 topic 对不上"的链路断裂没被发现。教训：

- 验证异步链路时，**必须从真实入口（HTTP 请求 → dispatcher → executor 失败）端到端触发**，而不是从 Kafka topic 中段手动注入。后者只能证明"消费者能处理消息"，证明不了"消息真会被生产"。
- 对每条 Kafka 链路，应单独 grep 确认 topic 名在生产者与消费者两端一致，而非依赖 smoke 脚本的硬编码 topic。

---

## 7. 文档与代码漂移（建议同步）

1. `docs/04-data-model.md §RLS` 的 GUC 名仍写旧的 `app.current_tenant_id` / `app.is_super_admin`，实际 DDL（`scripts/init-db/01-schema.sql:250`）与代码用 `app.tenant_id` / `app.is_platform_admin`（代码自洽，文档滞后）。
2. `docs/03-services.md §3.15` 按 5 渠道描述 notification，实现只 1 渠道（文档严重超前）。
3. `docs/03-services.md` 列 sdk-gen 为第 10 个服务，代码不存在（roadmap 排 Phase 3，未启动）。
4. ADR-006 的 Key 轮换（`last_rotated_at`、外部 Key 1 年默认）代码未落实。
5. ADR-013 多 Region 全双活的写亲和/双向复制，代码与脚本远未达到 ADR 描述（见 §3.7）。

---

## 8. 修复优先级建议

| 级别 | 项 | 工作量 | 触发 |
|---|---|---|---|
| **P0** | executor 失败补投 `task-failures`（救活整个重试链） | 小 | §2.1 |
| **P0** | retry `trigger` 补 `delay_queue.schedule`（救活手动重试） | 小 | §2.2 |
| **P0** | Phase 4 四服务补 Dockerfile + k8s Deployment + overlay | 中 | §2.3 |
| **P0** | `admin_db_session` 补审计写入（等保合规） | 中 | §2.4 |
| **P0** | `db_session` 改 `set_config($1)` + config 密钥加 prod 断言 | 小 | §2.5 |
| **P0** | CronJob 补 secret 卷挂载 | 小 | §2.6 |
| **P1** | api-registry 接 APISIX Admin API（闭环发布） | 中 | §3.1 |
| **P1** | notification 补 `/internal/notify/send` + 邮件/钉钉渠道 | 大 | §3.3 |
| **P1** | ai-gateway 明确定位：接入 dispatcher 或归档 | 中 | §3.4 |
| **P1** | 修 `withdraw_consent` 接 anonymize + Portal `/v1` 前缀 | 小 | §3.5/3.6 |
| **P1** | 多 Region：注入 consumer home_region / 修复制脚本 / 或明确降级 | 中-大 | §3.7 |
| **P1** | Go quota：补齐对齐+接入部署，或移除避免误导 | 中 | §3.8 |
| **P1** | dispatcher 事件 trace_id 改用 OTel span context | 小 | §3.9 |
| **P1** | auth 补 HMAC/OAuth2 或从设计降级 | 中 | §3.10 |
| **P2** | 各 stub 补齐（trace export/compare、docs 参数表、admin RBAC/CSV、billing 阶梯+CronJob） | 中 | §4 |

---

## 9. 架构层问题（design-level，与上面的实现 bug 区分）

§2-§4 是"实现层"问题（某端点没做 / 某行写错 / 某 stub 未兑现）。本节是"架构层"判断——**即使把实现 bug 都补上，这些结构性接缝仍会持续制造同类问题**。

**总评**：地基和方向是对的（网关 + 微服务 + 多存储分工 + RLS 多租户），常规且基本合理，不到推倒重来。但有 **2 个真缺陷 + 4 个张力**。

### 9.1 真缺陷

**(A) 路由所有权三重叠加，控制面/数据面没接通**
一个请求的路由真相分散三处：PG（api-registry 元数据）、Redis（dispatcher resolver 缓存，`dispatcher/resolver.py`）、APISIX（phase2 手工装的静态 route）。APISIX 做 `/dispatch/*` 路由 + key-auth，**dispatcher 又自己从 Redis re-resolve 一次**——两层路由逻辑、职责不清。api-registry 发布本是控制面（设计意图：→ APISIX Admin API 下发），实际是 stub（§3.1）。
→ **即使补上 stub，"APISIX 和 dispatcher 谁是真路由层"的重叠仍在。** 要么 dispatcher 退化为纯转发（路由全归 APISIX），要么 APISIX 只留鉴权/限流不做动态路由。当前两者各做一半。

**(B) 服务/聚合边界泄漏（ownership leak）**
设计 §3.5 说 auth 拥有 App/Key，但 **portal-bff 直写 `app`/`api_key` 表**（`portal/repository.py:25-92`）绕过 auth；admin 直写 audit；quota 与 billing 都从 CH 读用量算钱；trace 与 admin 都查调用日志。没有"谁拥有哪个聚合"的硬规则，多服务共写共读同一批表。
→ §2-§4 里大量集成 bug（jsonb 双重序列化、ID 类型漂移、字段不对齐）的**架构根因**就在这。BFF 应是聚合/转发层，不该越权直写领域服务的表。

### 9.2 张力（defensible，但要付税）

**(C) 每请求同步扇出**：网关 → auth → quota → dispatcher → 后端，4 跳串行 HTTP，目标 P99 < 200ms。auth/quota 靠缓存撑，但架构把延迟税 baked-in。团队拆分友好 vs 单请求延迟的经典权衡；若真要 5w QPS，auth/quota 更该是 sidecar（Envoy ext-auth / Lua 插件）。Go 重写 quota 是治标。

**(D) Kafka 异步主干无契约约束**：topic 是松散字符串，生产者/消费者各写各的，无 schema registry、无共享事件定义。**§2.1 的 retry 链断裂（task-failures 无生产者）能长期没被发现，根因就是架构层没有异步契约强制**。补一个 emit 治标，治本要共享 event schema + topic ownership 文档。

**(E) 多租户中央不变量在分析存储失效**：全系统以"RLS 是中央不变量"为核心（ADR-009），但 **ClickHouse 无 RLS，靠应用层自觉加 `WHERE tenant_id`**，库连护栏都没有（`ch_session` 不强制过滤，见 §4）。trace/高级分析的租户隔离是软约束——核心设计原则没贯穿到所有存储。

**(F) 过度拆分 + Phase 4 用"并行系统"代替"原地扩展"**：设计 §6 自己警告"别一上来拆 15 个"，代码却 15+ 服务，notification(254 行)/ai-gateway/billing 空壳或孤岛——premature distribution，运维成本已付收益没兑现。更糟的是 Phase 4 违背自己的 ADR 堆并行实现：
- **ADR-004 明确写"在 dispatcher/schema/quota/docs 预留 AI 扩展点"**，结果另起 ai-gateway 服务，且 dispatcher 还自己做 AI 流式——两条 AI 路径并存（§3.4）。
- Go quota 不是替换而是并存（Redis key schema 都不兼容，§3.8）。
- ADR-008（单 Region）7/14 被推翻成 ADR-013（全双活），重大架构反转仓促上马 → 半成品多 Region（§3.7）。
→ 架构治理缺位：没有单一 owner 守住设计契约，ADR 被新增服务绕过。

### 9.3 架构上没问题的（平衡）

- **存储分工**（PG 元数据 / CH 日志 / Redis 计数 / Kafka 事件 / MinIO 大对象）清晰合理。
- **三种任务模型统一走一个网关**（sync=dispatcher / 短异步=executor / 长 DAG=workflow）是好的抽象；workflow stub+真 Argo 双实现是亮点。
- **服务工厂统一**（create_app + tenant middleware + 统一错误/健康/OTel）横切设计好。
- **声明式 YAML + change-request 工单 + 分级审批**，治理流程扎实。
- **BFF 内/外分离**（admin vs portal）边界清晰（除 9.1-B 的越权写）。

### 9.4 小结

地基方向对，不推倒重来。但 **(A) 路由三重叠加** 与 **(B) 服务边界泄漏** 是两个真缺陷，会在后续每个集成点反复制造 bug；**(D) Kafka 无契约** 是 §2.1 retry 断链的根因，**(B) 边界泄漏** 是 §2-§4 一堆字段/序列化 bug 的根因。只补实现 bug 不修这两个架构缺陷，同类问题会持续长出来。

---

## 附录 A：审计范围与分工

- codegraph 索引：377 文件 / 3836 节点 / 6521 边（sync up-to-date）。
- 5 组并行审计 agent，覆盖：①apihub-core 共享库 ②api-registry+dispatcher ③auth+quota(Python)+Go quota ④executor+retry+workflow+trace ⑤portal+admin+docs+billing+ai-gateway+notification ⑥Phase 4 横切（GDPR/多 Region/高级分析）+ 测试健康度 + 部署/schema 集成。
- 每条结论均有 file:line 证据支撑；stub/TODO/空壳判定基于函数体（pass / raise NotImplementedError / return 假数据 / 无对应 DB 表 / 无 k8s manifest）。

## 附录 B：phase2 声称修复的坑复核结论

phase2 文档声称修复的以下坑，经独立复核**确已在代码里真修**：
- #11 OTel `FastAPIInstrumentor().instrument()` 实例方法（`tracing.py`）
- #13 auth 不再解 `["data"]` envelope（`auth.py:76`）
- #14 dispatcher `b"data:..."` 字节字面量（`forwarder.py:143`，但残留死函数 `_b`）
- #16/#21/#19 jsonb codec 双重序列化（db.py init_pool 注册；retry/repository.py、change_request.py 去掉手工 json 转换）
- #17 trace-svc SQL 列名对齐精简 CH schema（`repository.py:71`）
- #18 api_version 补 method/path 列（`models.py`/`routes.py`）
- #20 误挂 updated_at 触发器
- #22 executor 补 `/v1/internal/retry`（`main.py:120`）
- #23 retry worker 读 `body["succeeded"]` 判定成功（`worker.py:274`）
- workflow stub + 真 Argo CRD e2e
- test_kafka 4 个预存失败已修

**唯一例外**：phase2 "task #99 重试链路验证通过"基于手动注入，掩盖了 §2.1 的生产者缺失——这不是"坑没修"，而是"验证方法绕过了真实入口"。
