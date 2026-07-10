# Handoff — 真 Argo CRD e2e（完成 + 下轮种子）

> 跨会话交接。本轮已完成（分支 `feat/argo-crd-e2e`，待合并）。新会话若继续，从「## 下轮种子」挑一项，按 superpowers brainstorming 开。

## 本轮成果

- **分支**：`feat/argo-crd-e2e`，`2ca50e6`(main) `..6477d84`(HEAD)，12 commit（spec/plan + 7 task + T6 两轮 fix + final-review pin）。
- **Final whole-branch review（opus）**：Ready to merge = **Yes**；6/6 spec 成功标准全 met；集成连贯；RLS 不变式保持；无 Critical。
- **计划/账本**：`docs/superpowers/plans/2026-07-10-argo-crd-e2e.md`、spec `docs/superpowers/specs/2026-07-10-argo-crd-e2e-design.md`、SDD 账本 `.superpowers/sdd/progress.md`。

### 交付了什么
- kind 装真 **Argo Workflows v3.0.3**（manifest install.yaml，pns 执行器，argoexec 预载）；`argo_mode` 由 kind overlay 切 **k8s**（base 仍 stub）。
- **K8sArgoClient 端到端验证全绿**：submit/get_status/get_steps/cancel/logs(SSE) 真 Argo；MinIO 产物往返（Argo 原生 artifactRepository→`argo-artifacts` bucket）。
- dispatcher 补 cancel/resume/logs(SSE) 薄代理（`POST/GET /v1/jobs/{id}/{cancel|resume|logs}`），e2e 全走网关。
- **修了 4 个 e2e 暴露的真 bug**（均有 CI 单测钉死）：`_node_to_step` inputs/outputs list→dict、`get_status` stop-strategy→CANCELLED、`stream_logs` CRD /log→核心 pods/log、`stream_logs` step_name 子串→node-name 注解精确匹配。
- 新增 `scripts/kind/argo-setup.sh`（装 Argo + 镜像预拉 + artifactRepository，Argo 版本 pin `v3.0.3`）、`deploy/k8s/base/argo/argo-exec-rbac.yaml`、kind overlay `workflow-argo-mode.yaml`、bootstrap 起 MinIO、smoke `k8s-workflow-argo.py` + `k8s-workflow-minio.py`、单测 `test_argo_mapping.py` + `test_k8s_argo_client.py`（workflow 测试 40→含 K8sArgoClient get_status/stream_logs）。

## 下轮种子（follow-up，按价值排序）

1. **resume 走 argo-server**（最重要）：`K8sArgoClient.resume`（argo_client.py:295）打 CRD 子资源 `/resume`，但 Argo v3.0.3 CRD `subresources={}` 不注册 → 502。本轮按 spec §5.1 降为 smoke warning。真修须改走 argo-server REST `POST /api/v1/workflows/{ns}/{name}/resume`（`--auth-mode server`，带 SA token）。
2. **Argo re-pin v3.5.x**：可去掉 MinIO smoke 的 `sleep 2`（PNS 捕获竞争）、启用默认 emissary 执行器、**且 v3.5+ 可能注册 resume 子资源**（顺带闭合 #1）。评估时连带回归 smoke + artifact。
3. **httpx `verify=<str>` DeprecationWarning**：`K8sArgoClient.__init__`（argo_client.py:210）`verify=ca_cert_path` → 改 `ssl.create_default_context(cafile=...)`，消 8 个测试 warning + 修 prod deprecation。一行。
4. **smoke A 相断言 node-name 特征**：现用 step count 区分真/stub（5 vs 2），spec §5.1 原望断言 node 名带 Argo 特征；running 转换+log 内容已更强，优先级低。

## 关键约束 / gotchas（本轮实踩，承接上轮）

- **账号**：业务 `apihub_app`/`apihub_app_dev_pwd`（NOSUPERUSER，走 RLS）；superuser `apihub`/`apihub_dev_pwd`。compose superuser 硬编码 `apihub`。
- **ID 类型**：`workflow_instance.id` 是 **int**；`api_id/app_id/tenant_id` 是 **str**。
- **namespace**：`apihub-workflow`（**单数**）。
- **Argo v3.0.3 特定**（re-pin 前适用）：① pns 执行器（containerd/kind 无 docker.sock）；② minio-go 不接受 endpoint 带 scheme（须裸 host:port + `insecure:true`）；③ CRD 无 /resume、/log 子资源（resume 走 argo-server，logs 走核心 pods/log）；④ install.yaml 无 Namespace（argo-setup 显式 create + `-n argo`）；⑤ controller 用两段式 `--configmap NAME`、`--executor-image IMG`（脚本 dual-form 检测）；⑥ argo-minio-secret 须在 `argo` **和** `apihub-workflow` 两 ns（executor 在后者挂载）。
- **MinIO**：host 端口 **29000**（9000/19000 被占）；`apihub`/`apihub_dev_pwd`；bucket `argo-artifacts`（+ call-bodies/sdk-packages/audit-archive/tfstate）。
- **代理坑**：`https_proxy=http://127.0.0.1:12348` 转发外网全挂；外网命令加 `env -u ..._PROXY` + `curl --noproxy '*'`；`uvx ruff==0.6.9` 装不了时用 `.venv-t1/bin/ruff`（CI 以 0.6.9 为准）。git over SSH 正常。
- **镜像**：kind 节点继承 host 代理、容器内拉不到 → host `docker pull` 后 `kind load`；build context 是**仓库根**。**改了 workflow/dispatcher 源码必须 rebuild+load+rollout restart**（否则跑旧镜像）。
- **不要 raw 重 apply overlay**：`shared-infra.yaml` 有 `__HOST_IP__`/端口占位符，bootstrap 运行时替换；raw `kustomize build|kubectl apply` 会用未替换值覆盖 live ConfigMap → 打断全集群 PG/Redis 连接。单文件 apply（如 `kubectl apply -f deploy/k8s/services/workflow/deployment.yaml`）OK。
- **kind 复用**：`scripts/kind/bootstrap.sh` 现含 MinIO 起带 + argo-setup 调用 + workflow k8s 重建（一键起真 Argo 栈）。host compose 数据层 = PG `apihub-pg`、Redis `apihub-redis`、Kafka `apihub-kafka`、CH、Jaeger `:16686`、OTel、MinIO `apihub-minio:29000`。

## 环境（新会话先验证还活着）
- kind `kind-apihub`：现跑真 Argo（pns）+ 12 apihub pods + MinIO。`kubectl --context kind-apihub get nodes`；没了 `bash scripts/kind/bootstrap.sh` 重建（会装 Argo）。
- 本地 venv `.venv-t1/`（未跟踪）：editable 装 apihub-core + 各 service，ruff 0.15.20 / pytest 9.1.1 / mypy 2.2.0。CI 以 ruff 0.6.9 为准。
- 真 Argo e2e smoke：`env -u ..._PROXY python3 scripts/smoke/k8s-workflow-argo.py`（A/B/D 绿，C resume warning）、`python3 scripts/smoke/k8s-workflow-minio.py`（全绿）。
