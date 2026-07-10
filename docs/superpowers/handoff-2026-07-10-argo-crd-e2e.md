# Handoff — 真 Argo CRD e2e（下一轮）

> 跨会话交接。新会话第一句：「读 `docs/superpowers/handoff-2026-07-10-argo-crd-e2e.md`，按 superpowers brainstorming 开『真 Argo CRD e2e』这轮」。

## 项目
APIHub —— 企业 API 网关/中台。Python 3.11 + FastAPI 微服务，**asyncpg 直连（非 SQLAlchemy）**，PostgreSQL **RLS 多租户**，ClickHouse/Kafka/Redis/APISIX，部署 Alibaba ACK。详见 `CLAUDE.md`（⚠️ 父目录有个无关的 Yorozuya CLAUDE.md，**不适用**本 repo）。工作目录 `/home/applo/project/ai-system-arch`。

## 当前状态
- `main` = `9a88979`（干净，与 origin 同步；只剩 `main` 分支）。
- 已合并：PR #9（Phase 3 P1：traceparent/cross-ns/workflow **stub** e2e）、PR #8（P0 技术债 + kind 全量验证 = `031f588`）、**PR #10（`9a88979`，清 PR #9 遗留 F2/F3/F5）**：
  - F2 CI `test.yml`：SQL 加载顺序改 `00→01→02→03→04→99`（99 跑最后）+ 补 load 03/04，修了原「99 跑在 02 前」的顺序 bug。
  - F3 spec Task A **勘误已落地**：你读 spec 会看到修正版——`consumer.py:73` 早已包 `consume_with_trace`，真断点是 executor 未接 OTel + `_call_backend` 未注入 W3C。**本轮 Argo e2e 不受影响**。
  - F5 dispatcher `/v1/jobs` 改 Pydantic → 422。
- **本轮目标**（上轮 spec/plan 的「Out of Scope / 下轮」）：在 kind 装真 Argo Workflow，把 workflow-svc 从 `argo_mode=stub` 切到 k8s，**端到端验证 `K8sArgoClient`**（提交真 CRD → Argo controller 起 Pod 跑 step → 轮询到 Succeeded → `get_status`/`get_steps` 拿到真实 Argo 数据）。

## 已有实现（别重做）
- **workflow-svc**：`services/services/workflow/src/workflow_svc/argo_client.py` —— `K8sArgoClient`（打 Argo CRD `/apis/argoproj.io/v1alpha1/namespaces/{ns}/workflows`，读 in-cluster SA token `/var/run/secrets/.../token`，`verify=ca.crt`）+ `StubArgoClient`（内存状态机，恒 running）。路由 `POST/GET /v1/workflows[/{id}]`、cancel/resume/steps/logs(SSE)。
- **dispatcher `/v1/jobs`**（PR #9）：代理 workflow-svc `POST /v1/workflows` + `GET /v1/workflows/{id}`。APISIX 路由 `/v1/jobs` + `/dispatch/`。
- **`workflow_instance` 表**：`scripts/init-db/04-phase3.sql`（PR #9，含 RLS）。
- 文档：`docs/05-core-flows.md` §4（workflow 时序：`POST /v1/jobs` mode:workflow → dispatcher → workflow-svc → Argo → MinIO）。

## 本轮核心任务
1. **装 Argo Workflow**：kind 装 CRD + controller（helm 或 manifest；仿 `scripts/kind/apisix-setup.sh` 的 helm 装 APISIX+etcd 模式）。
2. **workflow-svc SA + RBAC**：`deploy/k8s/services/workflow/deployment.yaml` 加 ServiceAccount；Role（create/get/list/patch `workflows.argoproj.io` + pods/pods/log）+ RoleBinding，绑到 `apihub-workflow` ns。`K8sArgoClient` 靠 in-cluster SA token。
3. **argo_mode 切 k8s**：kind overlay 把 workflow-svc 的 `ARGO_MODE`（**先确认 setting 名**，看 `workflow_svc/main.py` + `apihub_core/config.py`）从 stub 改 k8s。
4. **可跑镜像**：workflow spec 用的镜像（busybox 等）kind 里要拉得到（公开镜像 OK）。
5. **e2e smoke** `scripts/smoke/k8s-workflow-argo.py`（仿 `k8s-workflow.py`）：经 APISIX `POST /v1/jobs` 真 Argo spec（busybox echo）→ 轮询 `GET /v1/jobs/{id}` 到 `succeeded` → 断言 steps 真跑过（**区别于 stub**：验真 Argo phase 转换 + steps 来自 Argo nodes）。
6. 验 `K8sArgoClient.get_status`（Argo phase→WorkflowStatus 映射）、`get_steps`（nodes→steps）、`cancel`（`spec.shutdown: Stop`）真生效。

## 同轮可做（或拆下下轮）
- MinIO 产物上传/下载 e2e（Argo step 产物 → MinIO）。
- cancel/resume/logs(SSE) e2e。

## 关键约束 / gotchas（上轮踩过）
- **账号**：业务 `apihub_app`/`apihub_app_dev_pwd`（NOSUPERUSER，走 RLS）；superuser `apihub`/`apihub_dev_pwd`。compose 里 superuser 硬编码 `apihub`（不能从 env 读，否则 RLS 失效）。
- **ID 类型**：`api_id/app_id/tenant_id` 是 **str**（text）；`workflow_instance.id` 是 **int**（bigserial）。
- **namespace = `apihub-workflow`（单数）**；Argo Workflow 跑这个 ns。
- **dispatcher 透传 X-API-Key 给 workflow-svc**（同 tenant 鉴权，已实测 OK）。
- **APISIX**：route 用 `uris:["/v1/jobs","/v1/jobs/*"]`（单 `/v1/jobs/*` 对 `POST /v1/jobs` 会 404）。NodePort 30080，key-auth `X-API-Key`，admin key 见 `scripts/kind/apisix-setup.sh`（从 APISIX cm 读，兜底 `edd1c9f0...`）。
- **镜像 build**：tag `registry.apihub.internal/apihub/<svc>:0.1.0-dev`（**确认 deployment 用的实际 tag**）；build context 是**仓库根**，不是 service 目录。
- **ruff 0.6.9**（CI `ruff==0.6.*`；本地 `uvx --with ruff==0.6.9 ruff ...`，**format 也要跑** `ruff format --check`，CI 两个都查）；**OTel 1.40.0 + instrumentation 0.61b0**（配对，别漂移）。
- **jsonb**：asyncpg 池注册了 jsonb codec → dict 直传，**别** `json.dumps`+`::text`+`json.loads`（双重编码 bug，findings #19/#21）。
- **CI gap**：`.github/workflows/test.yml` 只 load init-db 00/01/99/02，**不** load 03/04 phase SQL（pre-existing；workflow 测试 stub repo 故未受影响）。本轮加 05+ 同理，记得评估是否补 CI。
- **kind 复用**：`scripts/kind/bootstrap.sh`（探测 host 网桥 IP、起 compose 数据层、建 kind、load 镜像、apply overlay）。数据层 = host docker-compose（PG `apihub-pg`、Jaeger `apihub-jaeger` :16686、Kafka `apihub-kafka`、CH、MinIO、OTel）。
- **网络/代理坑**（2026-07-10 实踩，可能仍相关）：shell 设了 `https_proxy=http://127.0.0.1:12348`。本会话该代理转发外网**全挂**（`curl api.github.com` 超时；`uvx`/`gh`/`pip` 全 connection reset），但 **git over SSH 正常**、**直连 443 正常**（`curl --noproxy '*' https://api.github.com` → 200）。绕法：命令前加 `env -u HTTPS_PROXY -u HTTP_PROXY -u https_proxy -u http_proxy -u ALL_PROXY -u all_proxy`（curl 用 `--noproxy '*'`）。**开 PR / 查 CI / `uvx ruff` 都得这么绕**。先试代理，挂了再绕。

## 环境（新会话先验证还活着）
- kind 集群 `kind-apihub`（上轮 12 pods Running，但已过数小时）：`kubectl --context kind-apihub get nodes`；没了就 `bash scripts/kind/bootstrap.sh` 重建。
- host compose：`docker compose -f docker-compose.dev.yml ps`。
- **本地 venv**（省网/省时）：`.venv-t1/`（仓库根，未跟踪）已 editable 装 apihub-core + 各 service（含 dispatcher），自带 ruff 0.15.20 / pytest 9.1.1 / mypy 2.2.0。代理挂、`uvx ruff==0.6.9` 装不了时，可用它近似验证（`pytest`/`ruff`/`mypy`，**CI 以 0.6.9 为准**）。workflow-svc 是否已装先 `import` 验一下，缺则 `pip install -e services/services/workflow`（需网）。

## 流程（按这个走）
1. `superpowers:brainstorming` → 探查现状（Argo 是否已装、workflow-svc SA/RBAC 现状、argo_mode setting 名、可跑镜像）→ 设计 → spec `docs/superpowers/specs/2026-07-1x-argo-crd-e2e-design.md`。
2. `superpowers:writing-plans` → `docs/superpowers/plans/2026-07-1x-argo-crd-e2e.md`。
3. `superpowers:subagent-driven-development` 执行（fresh subagent/任务 + review；进度账本 `.superpowers/sdd/progress.md`）。
4. 新分支 `feat/argo-crd-e2e` off main。
- 参考上轮：spec `docs/superpowers/specs/2026-07-10-phase3-p1-validation-design.md`、plan `docs/superpowers/plans/2026-07-10-phase3-p1-validation.md`、findings `docs/phase2-integration-findings.md`（carry-forward）。
