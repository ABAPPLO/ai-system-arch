# Design Spec — 真 Argo CRD e2e（核心 + 全部 extras）

- **日期**：2026-07-10
- **分支**：`feat/argo-crd-e2e`（off `main` = `9a88979`）
- **上轮交接**：`docs/superpowers/handoff-2026-07-10-argo-crd-e2e.md`
- **状态**：已通过 brainstorming 评审，待 writing-plans

## 1. 背景与目标

APIHub 的 `workflow-svc` 已实现 `K8sArgoClient`（httpx + in-cluster SA token，打 Argo Workflow CRD），但 kind 一直以 `argo_mode=stub` 跑（`StubArgoClient` 内存状态机），从未端到端验证过真 Argo 路径。

**本轮目标**：在 kind 装真 Argo Workflow，把 workflow-svc 从 `stub` 切到 `k8s`，**端到端验证 `K8sArgoClient` 的全部能力**——提交真 CRD → Argo controller 起 Pod 跑 step → 轮询到 Succeeded → `get_status`/`get_steps` 拿到真实 Argo 数据；并覆盖 cancel/resume/logs(SSE)/MinIO 产物往返。

**成功标准**（可执行断言）：
1. `argo-setup.sh` 后：Argo CRD 已装、controller Running、`argo-exec` SA + pods RBAC 存在、MinIO 产物仓库可配。
2. workflow-svc 以 `argo_mode=k8s` 起，`/health/ready` ready。
3. `k8s-workflow-argo.py` 全绿：真 Argo wf 跑到 `succeeded`（经历 running→succeeded 真转换，非 stub 瞬时 running）；steps 来自 Argo nodes（真 node 名）；cancel/resume/logs(SSE) 各自子断言通过。
4. `k8s-workflow-minio.py` 全绿：Argo `outputs.artifacts` 写入 MinIO，verify 步骤读到一致内容。
5. `test_argo_mapping.py` 单测绿（CI 可跑）。
6. base configmap `ARGO_MODE` 仍为 `stub`（改动隔离在 kind overlay）。

## 2. 现状（探查结论）

| 项 | 状态 |
|---|---|
| kind `kind-apihub` | ✅ alive（12 pods） |
| `apihub-workflow` ns | ✅ 存在；仅有 tokenless `default` SA |
| Argo CRD + controller | ❌ 未装 |
| `argo_mode` setting | `config.py:47` `argo_mode`（默认 stub）+ `k8s_api_server`；env `ARGO_MODE`/`K8S_API_SERVER` |
| kind overlay 切 k8s | ❌ 未接（configmap 硬编码 `ARGO_MODE: stub`，无 patch） |
| `K8sArgoClient` | ✅ 完整：submit/get_status/cancel/resume/get_steps/stream_logs + `_phase_to_status`/`_node_to_step`（`argo_client.py`） |
| svc `workflow` SA + Role(`workflows`/`workflows/log`) + RoleBinding | ✅ 已在 `deployment.yaml` 且已 apply |
| dispatcher `/v1/jobs` 代理 | ✅ 已落地（PR #9） |
| MinIO（compose `apihub-minio`:9000） | ✅ 存在；bootstrap **跳过**（端口冲突） |
| Argo artifactRepository 配置 | ❌ 无；apihub-core 无 S3 client |
| 镜像预拉绕法 | ✅ 现成（apisix-setup.sh #5：host `docker pull` → `kind load`） |

## 3. 设计决策

### 3.1 Argo 安装方式 —— 官方 manifest install.yaml
`curl --noproxy '*'` 拉 `https://raw.githubusercontent.com/argoproj/argo-workflows/stable/manifests/install.yaml` → `kubectl apply`。装在 `argo` ns，manifest 自带 ClusterRole/ClusterRoleBinding → controller watch 全 ns（含 `apihub-workflow`）。**脚本驱动、kind-only**——不进提交的 `deploy/k8s`（prod 用 ACK 集群内 Argo，提交 manifest 会误导）。

镜像（`argoproj/argocli`、`workflow-controller`、`argoexec`，版本随 install.yaml）+ busybox：host `docker pull` 后 `kind load`（kind 节点继承 host 代理，容器内不可达 docker.io，同 apisix 坑）。

### 3.2 argo_mode 切 k8s —— kind overlay ConfigMap patch
新增 `deploy/k8s/overlays/kind/patches/workflow-argo-mode.yaml`（strategic-merge，target ConfigMap `workflow-config`，`data.ARGO_MODE: k8s`），加进 `kustomization.yaml` patches。
- base `configmap.yaml` 保持 `ARGO_MODE: stub` → staging/prod 未建 overlay 不受影响。
- apply 后须 `kubectl -n apihub-system rollout restart deploy/workflow`：当前 pod（9h 前）以 stub 起；重启后 `argo_lifespan`（`main.py:18`）读 `argo_mode=k8s` → 初始化 `K8sArgoClient`。

### 3.3 Argo executor RBAC —— 独立 `argo-exec` SA
两个 SA 职责分离：
- **`workflow`**（ns `apihub-system`，已有）：workflow-svc 进程 ↔ Argo CRD 的 API 调用（已有 `workflows`/`workflows/log` 权限）。
- **`argo-exec`**（ns `apihub-workflow`，新增）：Argo controller 在 `apihub-workflow` 起的 step Pod 以此 SA 跑；executor（argoexec/emissary）需 `pods` 权限回报结果。

`deploy/k8s/base/argo/argo-exec-rbac.yaml`（ns `apihub-workflow`）：
```yaml
apiVersion: v1
kind: ServiceAccount
metadata: { name: argo-exec, namespace: apihub-workflow }
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata: { name: argo-exec, namespace: apihub-workflow }
rules:
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get","list","watch","create","update","patch","delete"]
  - apiGroups: [""]
    resources: ["pods/log"]
    verbs: ["get"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata: { name: argo-exec, namespace: apihub-workflow }
subjects: [{ kind: ServiceAccount, name: argo-exec, namespace: apihub-workflow }]
roleRef: { kind: Role, name: argo-exec, apiGroup: rbac.authorization.k8s.io }
```
进 `base/`：凡跑 Argo 的环境（kind + 未来 staging/prod）都需要。smoke spec 里写 `spec.serviceAccountName: argo-exec`。

### 3.4 MinIO 可达性 + Argo 产物仓库 —— Argo 原生 artifact
- **bootstrap 起 MinIO**：数据层 bring-up 加 `minio` + `minio-init`，端口走 `pick_host_port`（默认 9000，冲突则 +10000 起寻空），同 redis/pg read-back 不变式（compose publish 端口 == overlay 写入端口）。`apihub-shared-infra` 写 `MINIO_ENDPOINT=http://$HOST_IP:$MINIO_HP`。
- **Argo artifactRepository**：`argo-setup.sh` patch `argo` ns 的 `workflow-controller-config-map`，设 `artifactRepository.s3`（endpoint=`http://$HOST_IP:$MINIO_HP`，bucket=Argo 专用如 `argo-artifacts`，`insecure: true`，path-style；accessKey/secretKey 从 Secret 引用）。bucket 由 `minio-init`/argo-setup 预建。
- **产物往返走 Argo 原生**：step A `outputs.artifacts`（写 MinIO）→ step B `inputs.artifacts`（Argo executor GET）。**不新写 apihub-core S3 client**（贴合架构，省代码）。

### 3.5 smoke 结构 —— one-smoke-per-concern
对齐现有 `k8s-trace.py`/`k8s-traceparent.py`/`k8s-links.py`/`k8s-workflow.py`：
- `scripts/smoke/k8s-workflow-argo.py`（headline）：succeeded 主路径 + cancel/resume/logs 子断言（共享 submit 路径，串行）。
- `scripts/smoke/k8s-workflow-minio.py`：产物往返（独立，因需 artifact spec + MinIO 校验逻辑）。

### 3.6 dispatcher 补 cancel/resume/logs 代理
现状 dispatcher 只代理 POST `/v1/jobs`、GET `/v1/jobs/{job_id}`（`dispatcher/routes.py:96,124`）。为让 cancel/resume/logs(SSE) 的 e2e 也走完整网关路径（APISIX → dispatcher → workflow-svc），新增 3 个薄代理（镜像现有 2 个：`_wf_client` + `settings.workflow_service_url` + 透传 `X-API-Key`）：
- `POST /v1/jobs/{id}/cancel` → workflow-svc `POST /v1/workflows/{id}/cancel`
- `POST /v1/jobs/{id}/resume`  → workflow-svc `POST /v1/workflows/{id}/resume`
- `GET  /v1/jobs/{id}/logs`    → workflow-svc `GET  /v1/workflows/{id}/logs`（SSE 透传，`StreamingResponse`）

APISIX route `uris:["/v1/jobs","/v1/jobs/*"]`（handoff §39）已覆盖这些子路径，无需改 APISIX。备选（smoke 直接 port-forward workflow-svc）否决：不走网关则不算端到端。

## 4. 组件 / 文件改动

| 类型 | 路径 | 内容 |
|---|---|---|
| 新增 | `scripts/kind/argo-setup.sh` | 拉 install.yaml（--noproxy）+ apply + 等 controller ready + host-pull/kind-load 镜像 + 配 artifactRepository(MinIO) + 自检 |
| 新增 | `deploy/k8s/base/argo/argo-exec-rbac.yaml` | §3.3 SA+Role+RoleBinding |
| 新增 | `deploy/k8s/overlays/kind/patches/workflow-argo-mode.yaml` | ConfigMap patch `ARGO_MODE: k8s` |
| 改 | `deploy/k8s/overlays/kind/kustomization.yaml` | resources +`base/argo/argo-exec-rbac.yaml`；patches +`workflow-argo-mode`（target ConfigMap/workflow-config） |
| 改 | `scripts/kind/bootstrap.sh` | 数据层 +minio/minio-init（pick_host_port + read-back 写 overlay MINIO 端口）；apply 后调 `argo-setup.sh`；rollout restart workflow |
| 改 | `deploy/k8s/overlays/kind/shared-infra.yaml` | 加 `MINIO_ENDPOINT`/`MINIO_USER`/`MINIO_PASSWORD`/bucket（由 bootstrap sed 写 HOST_IP + 端口） |
| 改 | `services/services/dispatcher/src/dispatcher/routes.py` | §3.6 补 cancel/resume/logs 三个薄代理（含 SSE 透传） |
| 新增 | `scripts/smoke/k8s-workflow-argo.py` | headline smoke（见 §5） |
| 新增 | `scripts/smoke/k8s-workflow-minio.py` | 产物往返 smoke（见 §5） |
| 新增 | `services/services/workflow/tests/test_argo_mapping.py` | `_phase_to_status`/`_node_to_step` 单测（真 Argo status JSON fixture） |
| 改 | `docs/05-core-flows.md` §4 | 注明 kind 现跑真 Argo + artifact 走 MinIO |

## 5. smoke 断言细节

### 5.1 `k8s-workflow-argo.py`
经 APISIX `:30080`（key `ak_test_a_demo001`）→ dispatcher → workflow-svc。seed `smoke-wf-api`（复用 stub smoke 的 seed）。

- **succeeded 主路径**：POST `/v1/jobs`（spec：entrypoint main → steps[s1,s2]，template echo=busybox echo hi，`serviceAccountName: argo-exec`）→ 断言 201 + `id`。轮询 GET `/v1/jobs/{id}`（最多 ~120s）到 `status=="succeeded"`。
  - 真转换断言：过程中至少观测到一次 `running`（区别 stub 的瞬时；若首轮即 succeeded 则放宽）。
  - steps 真实断言：`steps` 非空且含 phase `succeeded` 的 node；至少一个 step 的 name 带真 Argo node 特征（非纯 template 名 "echo"）。
  - get_status/get_steps 映射：隐式由上述 status/steps 验证。
- **cancel 子测**：提交长跑 wf（busybox `sleep 300`，SA=argo-exec）→ 轮询到 `running` → POST `/v1/jobs/{id}/cancel`（APISIX→dispatcher→workflow-svc）→ 轮询到 `cancelled`（Argo phase=Stopped 映射）。断言不再变 succeeded。
- **resume 子测**：对一 `sleep` + 故意 `suspend` 的 wf → POST `/v1/jobs/{id}/resume` → 断言回到 `running`/可继续。（若 Argo suspend 语义复杂，降级为：resume 端点 200 + 不报错，记为软断言。）
- **logs(SSE) 子测**：succeeded wf → GET `/v1/jobs/{id}/logs`（EventStream，APISIX→dispatcher 透传 SSE）→ 读若干帧 → 断言含 `hi`（busybox echo 真输出），区别 stub 的 `[name] step started`。

退出码同 stub smoke：0 OK / 1 assert / 2 env-unavailable。

### 5.2 `k8s-workflow-minio.py`
- spec：step A（busybox 写 `/tmp/out.txt` 内容 + `outputs.artifacts` 指向 `/tmp/out.txt`）→ step B（`inputs.artifacts` 接收 + 校验）。
- 断言：wf `succeeded`；B 步成功读到 A 产物（Argo 经 MinIO 中转）；可选：`mc`/minio API 直查 bucket 存在对象。
- 若 artifactRepository 配置在 kind 不稳（path-style/虚拟主机），降级为：wf succeeded + Argo 无 artifact Error，并把 MinIO 直查作为软断言。

## 6. 错误处理 / 复用

- `K8sArgoClient` 已把 httpx 错误包成 `ArgoError`、routes 已映 502 —— **本轮不改业务错误路径**。
- argo-setup.sh 须先消解的失败模式（否则 smoke 报这些，不是产品 bug）：CRD 没装（submit 502/404）、`argo-exec` SA 缺权限（wf → Error「pods forbidden」）、busybox 未预拉（wf Failed ImagePull）、MinIO 不可达（artifact Error）。
- argo-setup.sh 自检门槛：`kubectl get crd workflows.argoproj.io` 存在、`argo` ns controller pod Ready、`argo-exec` SA 存在、artifactRepository ConfigMap 已 patch。

## 7. 风险 / gotchas

- **代理坑**（handoff §45）：install.yaml 用 `curl --noproxy '*'`；`docker pull`/`kind load` 走 host（host 直连或 docker daemon 自带代理）；`uvx ruff==0.6.9` 走 `.venv-t1` 兜底。开 PR/查 CI 前加 `env -u ..._PROXY`。
- **workflow pod 重启**：切 configmap 后必须 rollout restart，否则仍 stub。
- **MinIO path-style**：Argo s3 artifact 配置需 path-style（MinIO 不支持虚拟主机样式）—— `useBucketAsHost: false` + 显式 endpoint；argo-setup.sh 校准，主要不确定点。
- **Argo 版本漂移**：install.yaml 用 `stable` tag，固定版本更稳；argo-setup.sh 可 pin（如 `v3.5.x`）。
- **CI gap**（handoff §43）：新 smoke 是 kind-only，不进 CI；不扩 CI SQL load。`test_argo_mapping.py` 是 CI 内唯一新增。
- **suspend/resume 真实语义**：Argo resume 针对被 `suspend` 节点；若 e2e 构造 suspend 模板复杂，resume 降级为软断言（见 §5.1）。

## 8. 测试策略

- **单测**（CI）：`test_argo_mapping.py` —— 喂真 Argo `status` JSON（phase/nodes），断言 `_phase_to_status`/`_node_to_step` 输出正确。防 phase/node 解析回归，CI 可跑（不依赖 kind）。
- **e2e**（kind-only）：`argo-setup.sh` 自检 + 两个 smoke。文档化运行方式（handoff/Makefile）。
- **验证矩阵**：get_status(phase→status) / get_steps(nodes→steps) / cancel(shutdown Stop) / resume / logs(SSE) / MinIO artifact 各一条断言。

## 9. 不在本轮（out of scope）

- staging/prod overlay 的 Argo 接入（prod 用 ACK 集群内 Argo，另议）。
- workflow-svc 业务错误路径重构、Argo WorkflowTemplate 复用、DAG 复杂拓扑。
- CI 跑 kind e2e（需 self-hosted runner，另议）。
- argo-workflows Grafana/Prometheus 监控面板。

## 10. 后续（下轮种子）

- MinIO 产物若走 Argo 原生稳了，可加 apihub-core S3 client（业务侧上传 SDK 包/大 body）。
- Argo WorkflowTemplate + 参数化模板复用。
- workflow-svc 的 SSE logs 改真流式（现一次性拉）。
