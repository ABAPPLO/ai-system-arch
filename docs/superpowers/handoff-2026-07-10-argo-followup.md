# Handoff — 真 Argo follow-up（Phase 1 已交付，re-pin 延期）

> 跨会话交接。本轮闭合 PR #11 的 resume 502 功能缺口（Phase 1，已交付、待合并）；
> Argo v3.5.15 re-pin（Phase 2）因 manifest/控制器模型变化 + 网络不稳 **de-scope 延期**（spec D4 允许），记为独立下轮。

## 本轮成果（Phase 1，分支 `feat/argo-followup`）

- **分支**：`feat/argo-followup`，`411d08c`(main) `..4ce8fe2`(HEAD)，5 commit（spec + plan + 3 实现）。
- **核心目标达成**：闭合 PR #11 真 Argo e2e 留下的 **resume 502** 功能缺口 —— resume 改走 **argo-server REST**（`PUT /api/v1/workflows/{ns}/{name}/resume`），smoke C 由软 warning 改**硬断言**，v3.0.3 上 A/B/C/D 全绿（C resume→succeeded）。
- spec `docs/superpowers/specs/2026-07-10-argo-followup-design.md`、plan `docs/superpowers/plans/2026-07-10-argo-followup.md`、SDD 账本 `.superpowers/sdd/progress.md`。

### 交付了什么
- `K8sArgoClient` 加专用 argo-server httpx client（`_server_client`），**只**路由 resume；submit/get_status/get_steps/cancel/stream_logs 全留 CRD（全绿，不动）。
- **始终发 SA bearer token**（D2，适配 server/client auth mode）+ 显式 `--auth-mode server`（D7，确定性）。
- **httpx `verify=<str>` DeprecationWarning 修复**：CRD client `verify=ca_cert_path` → `ssl.create_default_context(cafile=...)`。
- Settings +`argo_server_url`/`argo_server_insecure`（`apihub_core/config.py`）；base configmap `ARGO_SERVER_URL`/`ARGO_SERVER_INSECURE:"false"`（prod）+ kind overlay `"true"`（dev 自签）。
- 单测 +3（resume via argo-server：URL/token/body/method、202、500→ArgoError）+ verify-warning 测试。

### 关键修正（live 踩坑，价值最大）
- **POST→PUT**：T1 resume 写成 POST，单测全绿（MockTransport 断言了 URL/token/body，**漏了 HTTP method**）；T2 真 Argo smoke 当场 501（`gRPC code 12 UNIMPLEMENTED`）。Argo grpc-gateway resume **只注册 PUT**。T2 实证 PUT→200→succeeded，折进 commit `5d53776`；controller 再 `4ce8fe2` 钉死 `assert method=="PUT"`。
- **教训**：纯单测（MockTransport）易漏 method/verb；live smoke 是 net。记入 ledger 当案例。

## De-scope：Argo re-pin v3.5.15（延期，独立下轮）

按 spec D4（re-pin 翻车不阻塞 Phase 1）de-scope。revert 了未 commit 的 argo-setup/minio 编辑（argo-setup 回 v3.0.3/pns、minio smoke 留 `sleep 2`）。**为什么延期**：

1. **v3.5.15 manifest 404**：`raw.githubusercontent.com/argoproj/argo-workflows/v3.5.15/manifests/install.yaml` **404**（`manifests/quick-start/postgres.yaml` 同），v3.0.3 的 URL 有效。v3.5 manifest 路径迁移或需 `kustomize build` 生成 —— **manifest 源本身未解决**。
2. **v3.5 controller 配置模型变**：install.yaml 里 controller `args: []`（无 `--configmap`/`--executor-image` flag）；镜像用 `:latest`（非 pin，`quay.io/argoproj/{argocli,workflow-controller}:latest`）；**argoexec 不在 install.yaml**（controller 用默认）→ argo-setup 的 executor-image 双形态预载会漏 argoexec → ImagePullBackOff。须显式预载 argoexec。
3. **网络/API 不稳**：本会话 Task 3 agent 死 2 次（sync 超时 + connection closed）+ install.yaml fetch flaky。

## 下轮种子（re-pin 独立轮，按顺序）

1. **解决 v3.5.x manifest 源**：找正确路径（可能 `manifests/dist/install.yaml` 或 `kustomize build manifests/cluster-install/`），或用 helm chart `argo/argo-workflows`（指定 tag）—— helm 更省心。
2. **argo-setup.sh 适配 v3.5 控制器模型**：configmap-name 探测落空 → 用默认 `workflow-controller-configmap`（已 fallback OK）；**显式预载 `quay.io/argoproj/argoexec:<tag>`**（install.yaml 不带）；镜像 `:latest` 改 pin（helm chart 或 sed 替换）。
3. **emissary + 去 sleep 2 + argo-server auth-mode**（本轮 argo-setup diff 思路已成型，下轮重做）。
4. 全 smoke 回归（A/B/C/D + MinIO）on v3.5.x；emissary 若报 `pods/exec forbidden` → argo-exec Role 加 `pods/exec`。
5. 顺带把 T2 暴露的 **kind 手动 apply 踩 overlay** 坑修了（base configmap/deploy 单文件 apply 会 revert overlay 的 ARGO_MODE/envFrom → pod crash）：用 overlay-rendered 或 `kubectl patch`，别 base 直 apply。

## 关键约束 / gotchas（承接 PR #11 + 本轮新增）

- **resume 必须 PUT**（不是 POST）—— Argo grpc-gateway 只注册 PUT。
- **argo-server server auth mode**（现 `args=["server"]`）：resume 免鉴权（argo-server 用自己 SA）；workflow-svc 仍发 SA token（D2，prod client-mode 兼容）。
- **workflow SA RBAC**：`workflow-argo-role`（`apihub-workflow` ns）已含 `workflows/resume` + `pods/pods/log`（deployment.yaml）。
- **kind 手动 apply 踩坑（本轮 T2 实踩）**：`kubectl apply -f deploy/k8s/services/workflow/configmap.yaml`（base）会 revert kind overlay 的 `ARGO_MODE=k8s`/`ARGO_SERVER_INSECURE=true` → workflow-svc 跑 stub；base deploy apply 会丢 envFrom（shared-infra/secret，PG_USER 源）→ pod crash `pg_user Field required`。**用 `kubectl patch`（merge）或 overlay-rendered，别 base 直 apply**。canonical 路径是 `scripts/kind/bootstrap.sh`（渲染 overlay + 替换 `__HOST_IP__`）。
- 其余（账号 `apihub_app`/`apihub_app_dev_pwd`、ID 类型 int/str、namespace `apihub-workflow` 单数、代理坑、镜像 rebuild+load+rollout、不 raw 重 apply overlay、ruff 0.6.9 / `.venv-t1` 兜底）同 PR #11 handoff。

## 环境（新会话先验证还活着）
- kind `kind-apihub`：真 Argo **v3.0.3**（pns）+ 12 apihub pods + MinIO。workflow-svc 2 pod Running（T2 已 rebuild+rollout，含 resume-via-argo-server）。`scripts/kind/bootstrap.sh` 重建。
- resume e2e smoke：`env -u ..._PROXY python3 scripts/smoke/k8s-workflow-argo.py`（A/B/C/D 全绿，C resume→succeeded 硬断言）。
