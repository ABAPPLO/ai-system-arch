# Handoff — 真 Argo re-pin v3.5.15（Phase 2 已交付，全绿）

> 跨会话交接。本轮把上轮 de-scope 的 **Argo re-pin v3.5.15**（emissary 执行器）落地：clean reinstall +
> 全 smoke（A/B/C/D + MinIO）on v3.5.15 全绿，Phase 1 的 resume-via-argo-server 功能保持。
> 分支 `feat/argo-repin-v3.5.15`，off `main`（`545baeb`）。

## 本轮成果

- **Argo v3.0.3 → v3.5.15**（emissary 执行器，去 PNS），kind `kind-apihub` clean reinstall。
- **smoke A/B/C/D + MinIO 全绿 on v3.5.15**；C（resume→succeeded）硬断言仍绿 = Phase 1 功能未回归。
- emissary 生效（wf pod 有 argoexec init container）、MinIO produce step 去 `sleep 2`（emissary 无 PNS 捕获竞态，亚秒容器退出即捕获产物，实测 artifact 经 MinIO 往返一致）。
- controller 稳定（0 restart），argo-server `args=["server"]`（auth-mode server）。

## 关键发现 / 对 handoff+spec 预期难点的正解（价值最大）

1. **manifest 源（404 破解）**：v3.5.15 raw `manifests/install.yaml` 确实 404（manifests 重组为
   `base/`/`cluster-install/`/`namespace-install/` kustomize + `quick-start-*.yaml` 单文件），但 **GitHub release
   提供 pre-built 单文件**：`https://github.com/argoproj/argo-workflows/releases/download/v3.5.15/install.yaml`。
   该 asset：镜像**版本-pin**（`argocli`/`workflow-controller:v3.5.15`，**非 `:latest`** — 纠正上轮 handoff 误判）、
   无 bundled minio、含全部 8 个 CRD（含 v3.5 新增 `workflowtasksets`/`workflowtaskresults`/`workflowartifactgctasks`）。
   v3.0.3 release 同样提供该 asset → **回滚 `ARGO_VERSION=v3.0.3` 走同一 URL**。
2. **`containerRuntimeExecutor` 字段已被 v3.5 移除（关键纠正 spec B.1）**：spec 写「configmap 设
   `containerRuntimeExecutor: emissary`」**是错的** —— v3.5 controller 严格 parser 拒绝该旧 string 字段
   （`json: unknown field "containerRuntimeExecutor"` → fatal → CrashLoopBackOff）。**正解：完全不设**
   （v3.5 默认 emissary，v3.3+ 起；Argo 自带 quick-start 的 configmap 也不设该字段）。argo-setup.sh 改为
   省略该字段 + 注释说明。
3. **argoexec 预载 fallback**：v3.5 controller `args:[]`（无 `--executor-image` flag）→ 上轮双形态探测落空。
   argo-setup.sh 新增 fallback：从 `workflow-controller` 镜像推导 `argoexec:同 repo 同 tag`
   （`quay.io/argoproj/argoexec:v3.5.15`），兜底硬编。controller 自动用同 tag argoexec。
4. **镜像拉取（host 代理坑，新）**：host docker daemon 配 `socks5://127.0.0.1:12348`（docker **仅支持
   HTTP-CONNECT，不支持 socks**）→ `quay.io` pull TLS handshake timeout。**不可重启 docker**（会杀 kind 集群 +
   minio）。解法：`crane pull`（via HTTP 代理 `12348`）→ `docker load` 预载 3 镜像（argocli/workflow-controller/
   argoexec），argo-setup.sh 见镜像在 daemon 则跳过 pull + 直接 `kind load`。crane 取自 go-containerregistry
   release `go-containerregistry_Linux_x86_64.tar.gz`。
5. **fetch 代理兜底（新）**：github release CDN（objects.githubusercontent.com）host 直连 flaky（TLS timeout）；
   argo-setup.sh fetch 改「先 `--noproxy` 直连，失败回退环境代理 `HTTPS_PROXY`」，跑时 `HTTPS_PROXY=http://127.0.0.1:12348`。

## 改动文件（分支 `feat/argo-repin-v3.5.15`）

| 文件 | 改动 |
|---|---|
| `scripts/kind/argo-setup.sh` | `ARGO_VERSION`→v3.5.15；fetch raw `manifests/install.yaml`(404)→release asset + 直连/代理兜底；argoexec 预载 fallback（args:[] 推导）；configmap 去 `containerRuntimeExecutor`（v3.5 移除）；注释随版本校准（pns→emissary、ns、secret、S3 endpoint watch-item） |
| `scripts/smoke/k8s-workflow-minio.py` | produce step 去 `&& sleep 2`（emissary 无 PNS 捕获竞态） |

> 7 处 v3.0.3 workaround 复核结论：no-quote image 正则、ns create+`-n argo` apply、argo-minio-secret 双 ns、
> no-scheme S3 endpoint（emissary 下仍 OK，实测 artifact 往返绿）**均仍适用**；executor-image 探测、
> configmap-name 探测落空 → 走新 fallback / 默认名；argo-server auth-mode 装好即 `server`（无需 patch）。

## 关键约束 / gotchas（承接前轮 + 本轮新增）

- **v3.5 configmap 不可写 `containerRuntimeExecutor`**（strict parser fatal）；emissary 是默认，别画蛇添足。
- **quay.io 镜像拉取须 crane 预载**（host docker daemon 的 socks5 代理 docker 不支持，重启 docker 会杀集群）。
  改 Argo 版本/补镜像：`HTTPS_PROXY=http://127.0.0.1:12348 crane pull quay.io/argoproj/<img>:<tag> /tmp/x.tar && docker load -i /tmp/x.tar`。
- **改 workflow 源码或重装 argo 后**：`kubectl -n apihub-system rollout restart deploy/workflow`（重连 argo-server）。
- **argo-server resume 必须 PUT**（承 Phase 1，未变）；server auth mode 下免鉴权，workflow-svc 仍发 SA token。
- clean reinstall 顺序：先 `kubectl -n apihub-workflow delete workflow --all`（活 controller 清 finalizer）→ 删 `argo` ns → 删 argoproj CRD → `argo-setup.sh`。回滚：`ARGO_VERSION=v3.0.3 ./argo-setup.sh`（release asset 同 URL）。
- 其余（账号 `apihub_app`/`apihub_app_dev_pwd`、ID int/str、ns `apihub-workflow` 单数、ruff 0.6.9 / `.venv-t1` 兜底、不 raw 重 apply overlay）同前轮。

## 环境（新会话先验证还活着）

- kind `kind-apihub`（context `kind-apihub`，**非** `kind-kind-apihub`）：真 Argo **v3.5.15**（emissary）+ 12 apihub pods + MinIO。
  workflow-controller / argo-server `argo` ns Running（0 restart）；`argo-server:2746` HTTPS 自签。
- workflow-svc = `apihub-system/deploy/workflow`（2 pod）。
- smoke：`env -u ..._PROXY python3 scripts/smoke/k8s-workflow-argo.py`（A/B/C/D 全绿）、`... k8s-workflow-minio.py`（全绿）。
- argo-setup 首跑（需拉 quay 镜像 / fetch）：`HTTPS_PROXY=http://127.0.0.1:12348 bash scripts/kind/argo-setup.sh`。

## Deferred / 后续

- prod argo-server 安全（client mode + 真 CA + mTLS）。
- Argo v3.6+ 升级评估。
- 上轮 T2 暴露的 **kind 手动单文件 apply 踩 overlay** 坑（base configmap/deploy apply 会 revert overlay 的 ARGO_MODE/envFrom）仍未根治 —— canonical 路径仍是 `scripts/kind/bootstrap.sh`（渲染 overlay + 替换 `__HOST_IP__`），别 base 直 apply。
