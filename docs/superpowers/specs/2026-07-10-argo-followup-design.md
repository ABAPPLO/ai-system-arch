# 真 Argo follow-up 收尾 —— resume via argo-server + Argo re-pin v3.5.15

> 设计文档（brainstorming 产物）。实现计划由 writing-plans 接续生成。
>
> 日期：2026-07-10。承接 PR #11（`ecec6ce`，真 Argo CRD e2e）的 4 个 follow-up。
> 分支：`feat/argo-followup`，off `main`（当前 `411d08c`）。

## 背景 / 动机

PR #11 把 workflow-svc 从 `argo_mode=stub` 切到真 Argo（v3.0.3），`K8sArgoClient` 的 submit / get_status / get_steps / cancel / stream_logs + MinIO 产物往返全绿，但留下 4 个 follow-up：

1. **resume 502（最痛）**：`K8sArgoClient.resume`（argo_client.py:295-304）打 CRD 子资源 `POST .../workflows/{name}/resume`，但 Argo Workflow CRD `subresources={}` 不注册该子资源 → 502。smoke C 按 PR #11 spec §5.1 软降为 warning。真修须改走 argo-server。
2. **httpx `verify=<str>` DeprecationWarning**：`K8sArgoClient.__init__`（argo_client.py:210）`verify=ca_cert_path` 传字符串 → httpx 新版 DeprecationWarning（8 个测试 warning + prod deprecation）。
3. **Argo v3.0.3 较旧**：re-pin v3.5.x → emissary 执行器（去掉 MinIO smoke 的 `sleep 2` PNS 捕获竞争）、更新的安全维护。
4. smoke A 相用 step count 区分真/stub（spec 原望断言 node-name 特征，优先级低）。

## 目标

- resume 真走通（经 argo-server），smoke C 由软 warning 改**硬断言**。
- httpx `verify=<str>` DeprecationWarning 消除。
- Argo re-pin **v3.5.15**，emissary 执行器，去 `sleep 2`，全 smoke 回归绿。
- 多租户 RLS 不变式保持（本轮不动 `db_session()` / `SET LOCAL app.tenant_id` 路径）。

## 非目标（Non-goals）

- prod argo-server 安全硬化（client mode + 真 CA + mTLS）—— 文档注明，非本轮。
- Argo 升到 v3.6+（钉 v3.5.x 稳定线）。
- resume 之外的 argo-server 操作（stop / resubmit / retry）—— 仅 resume，YAGNI。
- 把 CRD 路径操作（submit / get_status / get_steps / cancel / stream_logs）搬到 argo-server —— 全绿，不动。

## 已确认事实（设计依据）

- 集群 `kind-apihub` 活着（23h，v1.31.0）；argo-server 在 `argo` ns，1/1 Running，`args=["server"]`（**server auth mode**），svc `argo-server:2746` HTTPS（自签证书）。
- **resume 只能经 argo-server**：Argo 无纯 CRD 字段能解除 suspend；controller 的 resume 由 argo-server `POST /api/v1/workflows/{ns}/{name}/resume` 触发。v3.5 同样不注册 resume 子资源 → **argo-server 路径与版本无关**。
- workflow SA（`workflow` @ `apihub-system`）在 `apihub-workflow` ns 的 `workflow-argo-role` 已含 `workflows/resume`（deployment.yaml:125-127）+ `pods`/`pods/log`。server mode 下 argo-server 用自己 SA 执行，workflow SA token 仅在 prod client-mode 下作身份。
- **Emissary 是 v3.5 默认执行器**（v3.3 起默认），为 containerd/kind 设计（取代 Docker executor 在 kind 卡住的痛点）→ pns workaround + MinIO smoke `sleep 2` 都可去。
- `K8sArgoClient` 由 `argo_lifespan`（main.py:18-34）经 `init_argo_client(mode, api_server=settings.k8s_api_server)` 构造；`argo_mode` / `k8s_api_server` 定义在 `apihub_core/config.py:47-48` Settings。

## 关键决策

| # | 决策 | 理由 |
|---|---|---|
| D1 | resume 走 argo-server（**专用 client**），CRD 路径不动 | resume 无 CRD 路径；其余 CRD 操作全绿 |
| D2 | **始终发 SA bearer token**（与 CRD client 同 token） | 同时适配 server/client auth mode —— server mode 下 argo-server 忽略调用方身份、用自己 SA；client mode 下 token 即身份、SA 已有 `workflows/resume` RBAC。→ 直接化解 v3.5 auth-mode 不确定风险 |
| D3 | argo-server TLS `verify=False`（dev） | 自签证书；prod 该上 client mode + 真 CA（文档注明，非本轮） |
| D4 | 顺序：**先 resume（v3.0.3）再 re-pin（v3.5.15）** | resume 经 argo-server 与版本无关；先小变动闭合最痛缺口，再大变动 re-pin。即使 re-pin 翻车，resume 已交付 |
| D5 | re-pin executor `pns`→`emissary`，去 `sleep 2` | emissary 是 v3.5 默认、kind 原生，去 PNS 捕获竞争 |
| D6 | smoke C 软 warning → **硬断言**（exit 1） | resume 真走通后必须硬绿 |
| D7 | re-pin 后 argo-server 显式确保 `--auth-mode server` | 不依赖 install.yaml 默认（版本间可能漂移）；D2 使其 auth-mode 无关，双保险 |

---

## A. resume via argo-server

### A.1 Settings（apihub_core/config.py）

`argo_mode` / `k8s_api_server`（config.py:47-48）旁加：

```python
argo_server_url: str = "https://argo-server.argo:2746"
argo_server_insecure: bool = True  # dev：argo-server 自签证书；prod 改 False + 真 CA
```

### A.2 K8sArgoClient（argo_client.py）

- `__init__` 加参数 `argo_server_url: str = "https://argo-server.argo:2746"`、`argo_server_insecure: bool = True`。
- 新建 argo-server 专用 client：
  ```python
  self._server_client = httpx.AsyncClient(
      base_url=argo_server_url,
      headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
      verify=not argo_server_insecure,  # dev insecure=True → verify=False
      timeout=httpx.Timeout(connect=5.0, read=timeout, write=5.0, pool=5.0),
  )
  ```
- CRD client 的 `verify=ca_cert_path`（:210）→ `verify=ssl.create_default_context(cafile=ca_cert_path)`（顺带消除 httpx DeprecationWarning，#2；需 `import ssl`）。
- `resume`（:295-304）改走 argo-server：
  ```python
  async def resume(self, *, namespace: str, argo_name: str) -> None:
      # Argo CRD 不注册 resume 子资源 → 经 argo-server REST。
      # server mode 下 argo-server 用自己 SA 执行；token 适配 client mode（D2）。
      try:
          resp = await self._server_client.post(
              f"/api/v1/workflows/{namespace}/{argo_name}/resume", json={}
          )
      except httpx.RequestError as e:
          raise ArgoError(f"argo-server resume failed: {e}") from e
      if resp.status_code not in (200, 202):
          raise ArgoError(f"argo-server resume returned {resp.status_code}: {resp.text[:500]}")
  ```
- `close()`：先 `await self._server_client.aclose()`，再原 CRD client `aclose()`。

### A.3 main.py argo_lifespan（:24-29）

`init_kwargs` 追加：
```python
"argo_server_url": getattr(settings, "argo_server_url", "https://argo-server.argo:2746"),
"argo_server_insecure": getattr(settings, "argo_server_insecure", True),
```

### A.4 init_argo_client（argo_client.py）

`K8sArgoClient(...)` 调用透传新 kwargs（`argo_server_url` / `argo_server_insecure`）。

### A.5 configmap

- **base** `deploy/k8s/services/workflow/configmap.yaml`：加 `ARGO_SERVER_URL: https://argo-server.argo:2746`、`ARGO_SERVER_INSECURE: "false"`（prod 默认安全）。
- **kind overlay** `deploy/k8s/overlays/kind/patches/workflow-argo-mode.yaml`：加 `ARGO_SERVER_INSECURE: "true"`（dev 自签证书；`ARGO_SERVER_URL` 与 base 默认一致，可不重复）。

### A.6 smoke C 硬化（scripts/smoke/k8s-workflow-argo.py）

C 相（:158-205）去掉软 warning 分支：`st != 200` 直接 `assert` exit 1；resume 后 `poll(wf_c, {"succeeded","failed"})`，断言 `status == "succeeded"`（suspend 解除后 echo2 跑完）。`c_warning` / `ALL OK (with warning)` 分支删除。

---

## B. Argo re-pin v3.5.15

### B.1 argo-setup.sh

- 顶部 `ARGO_VERSION="${ARGO_VERSION:-v3.5.15}"`（更新顶部版本耦合注释：v3.5 默认 emissary，pns/sleep-2 已不适用）。
- artifactRepository ConfigMap `containerRuntimeExecutor: pns`（:125）→ `emissary`。
- **argo-server auth mode**：apply install.yaml 后复核 argo-server args；若非 `--auth-mode server`（v3.0.3 install.yaml 自带 server；v3.5 默认可能不同），`kubectl -n argo patch deploy argo-server` args 补 `--auth-mode server`（D7，确定性）。
- **复核 7 处 v3.0.3 workaround 在 v3.5.15 是否仍需/正确**（逐一验证，smoke 回归兜底）：
  - 无引号 image 正则（:57）：v3.5 quoted image → 现有 regex `("[^"]+"|[^[:space:]]+)` 已双形态，复核命中。
  - ns 显式 create + `-n argo` apply（:90-92）：仍 OK。
  - configmap 两参/等号双探测（:110-114）：复核 v3.5 controller args 形态。
  - executor-image 双形态探测（:68-76）：复核 v3.5 flag（可能改名/形态变）。
  - no-scheme S3 endpoint（:39）：bare host:port 始终接受，复核。
  - argo-server auth mode：上条 D7。
  - argo-minio-secret 双 ns（:98-106）：emissary executor 仍需 mount S3 凭证，保留。

### B.2 MinIO smoke（scripts/smoke/k8s-workflow-minio.py）

删 produce step 的 `&& sleep 2`（emissary 无 PNS 捕获竞争）。

### B.3 emissary RBAC 复核

emissary 基本执行（echo/sleep/busybox）无需额外 workflow-SA RBAC；若 smoke 报权限错（如 `pods/exec is forbidden`），给 `argo-exec` Role（`deploy/k8s/base/argo/argo-exec-rbac.yaml`）加 `pods/exec`。实现时按 smoke 报错处理（watch-item，非预设）。

---

## 出口标准

**Phase 1（当前 v3.0.3）**：
- `pytest services/services/workflow/ -v` 全绿（含新 resume-via-argo-server 单测）；httpx DeprecationWarning 消。
- smoke C（resume）on v3.0.3 **硬绿**（exit 0）。
- `ruff` + `mypy` workflow 全绿。

**Phase 2（re-pin v3.5.15）**：
- `scripts/smoke/k8s-workflow-argo.py`（A/B/C/D）+ `scripts/smoke/k8s-workflow-minio.py` 全绿。
- emissary 执行器生效（controller ConfigMap `containerRuntimeExecutor: emissary`，controller 日志或 ConfigMap 回读确认）。
- produce step 无 `sleep 2`。

## 风险与兜底

| 风险 | 概率 | 兜底 |
|---|---|---|
| v3.5.15 install.yaml 形态变化打破 image/configmap/executor 探测 | 中 | 7 处逐一复核；smoke 回归兜底；某处挂则针对性改探测正则 |
| emissary 在 kind 报 RBAC/权限错 | 低-中 | smoke 报错给 argo-exec Role 加 `pods/exec`（B.3 watch-item） |
| argo-server server mode 在 v3.5 行为差异 | 低 | D2 始终发 token + D7 显式 `--auth-mode server`，双保险 |
| re-pin 打破当前绿的 e2e | 中 | D4：Phase 1 先在 v3.0.3 闭合 resume（即使 Phase 2 翻车，resume 已交付）；Phase 2 翻车可回退 `ARGO_VERSION=v3.0.3`（revert argo-setup 改动） |
| argo-server 自签证书 verify=False 仍 httpx warning | 低 | dev 可接受；prod 上真 CA |

## 改动文件总览

| 文件 | 改动 |
|---|---|
| `services/libs/apihub-core/src/apihub_core/config.py` | +`argo_server_url` / +`argo_server_insecure` Settings 字段 |
| `services/services/workflow/src/workflow_svc/argo_client.py` | K8sArgoClient +`_server_client` + resume 走 argo-server + verify ssl context + close |
| `services/services/workflow/src/workflow_svc/main.py` | argo_lifespan 透传 argo_server_url/insecure |
| `deploy/k8s/services/workflow/configmap.yaml` | +`ARGO_SERVER_URL` / +`ARGO_SERVER_INSECURE: "false"`（base prod 默认） |
| `deploy/k8s/overlays/kind/patches/workflow-argo-mode.yaml` | +`ARGO_SERVER_INSECURE: "true"`（dev） |
| `scripts/kind/argo-setup.sh` | `ARGO_VERSION`→v3.5.15 + emissary + argo-server `--auth-mode server` + 复核 workaround |
| `scripts/smoke/k8s-workflow-argo.py` | smoke C 软 warning → 硬断言 |
| `scripts/smoke/k8s-workflow-minio.py` | 删 `sleep 2` |
| `services/services/workflow/tests/test_k8s_argo_client.py` | +resume via argo-server 单测（MockTransport） |

## Testing Strategy

- **单测**（test_k8s_argo_client.py）：`K8sArgoClient.resume` 经 httpx `MockTransport` 打 argo-server `/api/v1/workflows/{ns}/{name}/resume`，断言：200/202 通过、非 2xx raise `ArgoError`、`Authorization: Bearer` header 发出、POST 打到 `_server_client`（非 CRD `_client`）。`verify=ssl.create_default_context(...)` 不触发 DeprecationWarning（测试加 `-W error::DeprecationWarning` 局部断言，或 grep 测试输出无 warning）。
- **e2e**：Phase 1 smoke C（v3.0.3 resume 硬绿）；Phase 2 全 smoke 回归（v3.5.15）。
- 不新增 CI workflow（smoke 依赖 live kind 集群，与既有 k8s-*.py 一致）。

## Deferred / 后续

- prod argo-server 安全（client mode + 真 CA + mTLS）。
- resume 之外 argo-server 操作（stop / resubmit / retry）若将来需纯 server 触发。
- Argo v3.6+ 升级评估。
