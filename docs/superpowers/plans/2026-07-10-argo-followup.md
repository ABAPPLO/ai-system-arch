# 真 Argo follow-up 收尾 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 闭合 PR #11 真 Argo e2e 留下的 4 个 follow-up —— resume 走 argo-server、httpx verify 修复、smoke C 硬化、Argo re-pin v3.5.15（emissary、去 sleep 2）。

**Architecture:** resume 经 argo-server REST（`POST /api/v1/workflows/{ns}/{name}/resume`），K8sArgoClient 加专用 `_server_client`，CRD 路径不动；始终发 SA token（适配 server/client auth mode）。两阶段：Phase 1 在稳定 v3.0.3 闭合 resume + httpx + smoke 硬化；Phase 2 re-pin v3.5.15 + 全 smoke 回归。

**Tech Stack:** Python 3.11 / FastAPI / httpx / pytest（httpx MockTransport）/ Argo Workflows v3.0.3→v3.5.15 / kind K8s。

## Global Constraints

- **分支**：`feat/argo-followup`，off `main`（当前 `411d08c`）。每 task 独立 commit。
- **顺序**：Task 1（CI，无集群）→ Task 2（live kind，Phase 1）→ Task 3（live kind，Phase 2 re-pin）。T2/T3 链式需 live 集群。
- **Argo 版本**：Phase 1 = v3.0.3（现装）；Phase 2 re-pin = **v3.5.15**（`scripts/kind/argo-setup.sh` 顶部 `ARGO_VERSION`）。
- **argo-server**：`argo` ns，svc `argo-server:2746` HTTPS（自签证书），现 `server` auth mode。in-cluster DNS `https://argo-server.argo:2746`。
- **workflow SA**：`workflow` @ `apihub-system`；`workflow-argo-role`（`apihub-workflow` ns）已含 `workflows/resume` + `pods`/`pods/log`（`deploy/k8s/services/workflow/deployment.yaml`）。
- **namespace**：`apihub-workflow`（**单数**）；Argo controller/argo-server 在 `argo` ns。
- **账号**：业务 `apihub_app`/`apihub_app_dev_pwd`（NOSUPERUSER，走 RLS）；superuser `apihub`/`apihub_dev_pwd`。
- **ID 类型**：`workflow_instance.id` 是 **int**；`api_id`/`app_id`/`tenant_id` 是 **str**。
- **代理坑**：`https_proxy=http://127.0.0.1:12348` 转发外网全挂；外网命令（argo-setup fetch install.yaml、`docker pull`）加 `env -u HTTPS_PROXY -u HTTP_PROXY -u https_proxy -u http_proxy -u ALL_PROXY -u all_proxy` + `curl --noproxy '*'`。git over SSH 正常。
- **镜像**：改了 workflow 源码必须 `docker build` → `kind load docker-image` → `kubectl -n apihub-system rollout restart deploy/workflow`（否则跑旧镜像）。build context 是**仓库根**。
- **不要 raw 重 apply overlay**：`shared-infra.yaml` 有 `__HOST_IP__`/端口占位符，bootstrap 运行时替换；raw `kustomize build|kubectl apply` 会覆盖 live ConfigMap → 打断全集群 PG/Redis 连接。单文件 `kubectl apply -f <file>` OK。
- **RLS 不变式**：本轮不动 `db_session()` / `SET LOCAL app.tenant_id` 路径。
- **ruff**：CI 以 **0.6.9** 为准（`uvx ruff==0.6.9`）；装不了时用 `.venv-t1/bin/ruff`（0.15.20）。`make lint` / `make fmt` 走根 `pyproject.toml`。
- **kind 复用**：集群 `kind-apihub`（现跑真 Argo v3.0.3 + 12 apihub pods + MinIO）。没了 `bash scripts/kind/bootstrap.sh` 重建。

---

## Task 1: resume via argo-server + httpx verify 修复 + Settings（CI）

**Files:**
- Modify: `services/libs/apihub-core/src/apihub_core/config.py`（+Settings 字段）
- Modify: `services/services/workflow/src/workflow_svc/argo_client.py`（K8sArgoClient `_server_client` + `resume` + `verify` ssl + `close`）
- Modify: `services/services/workflow/src/workflow_svc/main.py`（`argo_lifespan` 加 init_kwargs）
- Modify: `deploy/k8s/services/workflow/configmap.yaml`（base +ARGO_SERVER_URL/INSECURE）
- Modify: `deploy/k8s/overlays/kind/patches/workflow-argo-mode.yaml`（dev +ARGO_SERVER_INSECURE）
- Test: `services/services/workflow/tests/test_k8s_argo_client.py`（+resume via argo-server + verify warning）

**Interfaces:**
- Consumes: `init_argo_client(*, mode, **kwargs)`（argo_client.py:445）已透传 `**kwargs` → `K8sArgoClient(**kwargs)`，**无需改**；只需 main.py 把新键塞进 `init_kwargs`。
- Produces: `K8sArgoClient.__init__` 新增 `argo_server_url`/`argo_server_insecure` 参数；`resume` 改走 `self._server_client`。

- [ ] **Step 1: 写失败测试（扩展 `_make_client` + resume 测试 + verify 测试）**

在 `test_k8s_argo_client.py`：

(a) `_make_client` 加可选 `server_handler` 参数（替换 `_server_client`）：

```python
def _make_client(handler, server_handler=None):
    """构造一个 K8sArgoClient，绕过 in-cluster token 读取 + 真 TLS。

    - 显式传 token 跳过 _read_sa_token。
    - 传真实 ca_cert_path（certifi）让 __init__ 的 CRD client 能构造。
    - MockTransport 替换 _client（CRD）+ 可选 _server_client（argo-server）。
    """
    import certifi

    c = K8sArgoClient(token="fake-token", ca_cert_path=certifi.where())  # noqa: S106
    c._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://k8s.test",
        headers={"Authorization": "Bearer fake-token", "Accept": "application/json"},
    )
    if server_handler is not None:
        c._server_client = httpx.AsyncClient(
            transport=httpx.MockTransport(server_handler),
            base_url="https://argo-server.argo:2746",
            headers={"Authorization": "Bearer fake-token", "Accept": "application/json"},
        )
    return c
```

(b) 文件末尾加 resume + verify 测试类：

```python
# ============ resume（via argo-server） ============


class TestResumeViaArgoServer:
    """resume 经 argo-server REST（D1）+ 始终发 SA token（D2）。"""

    async def test_resume_posts_to_argo_server_with_token(self):
        seen: dict = {}

        def server_handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization")
            seen["body"] = request.content
            return httpx.Response(200, json={"metadata": {"name": "wf-x"}})

        def crd_handler(request: httpx.Request) -> httpx.Response:  # resume 不该触达 CRD
            return httpx.Response(404)

        c = _make_client(crd_handler, server_handler)
        try:
            await c.resume(namespace="apihub-workflow", argo_name="wf-x")
            assert "/api/v1/workflows/apihub-workflow/wf-x/resume" in seen["url"]
            assert seen["auth"] == "Bearer fake-token"
            assert seen["body"] == b"{}"
        finally:
            await c.close()

    async def test_resume_202_accepted(self):
        def server_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(202, json={})

        c = _make_client(lambda r: httpx.Response(404), server_handler)
        try:
            await c.resume(namespace="ns", argo_name="wf-x")  # 不抛即过
        finally:
            await c.close()

    async def test_resume_500_raises_argo_error(self):
        from workflow_svc.argo_client import ArgoError

        def server_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        c = _make_client(lambda r: httpx.Response(404), server_handler)
        try:
            with pytest.raises(ArgoError, match="argo-server resume returned 500"):
                await c.resume(namespace="ns", argo_name="wf-x")
        finally:
            await c.close()


async def test_init_no_verify_deprecation_warning():
    """verify=<str> 已改 ssl context —— 构造不再触发 httpx DeprecationWarning。"""
    import certifi
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        c = K8sArgoClient(token="t", ca_cert_path=certifi.where())  # noqa: S106
    await c.close()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `/home/applo/project/ai-system-arch/.venv-t1/bin/pytest services/services/workflow/tests/test_k8s_argo_client.py::TestResumeViaArgoServer -x`

Expected: FAIL —— `test_resume_posts_to_argo_server_with_token` 因 resume 仍打 CRD `_client`（被 crd_handler 返回 404）→ raise `ArgoError`，而非命中 server_handler。

- [ ] **Step 3: 实现 Settings（config.py）**

`services/libs/apihub-core/src/apihub_core/config.py`，`argo_mode`/`k8s_api_server`（:47-48）后加两字段：

```python
    argo_mode: str = "stub"
    k8s_api_server: str = "https://kubernetes.default.svc"
    argo_server_url: str = "https://argo-server.argo:2746"
    argo_server_insecure: bool = True  # dev：argo-server 自签证书；prod 改 False + 真 CA
```

- [ ] **Step 4: 实现 K8sArgoClient（argo_client.py）**

(a) 顶部加 `import ssl`（与既有 `import httpx` 同区）。

(b) `K8sArgoClient.__init__`（:193-212）加参数 + 建 `_server_client` + CRD client verify 改 ssl context：

```python
    def __init__(
        self,
        *,
        api_server: str = "https://kubernetes.default.svc",
        token: str | None = None,
        ca_cert_path: str = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
        timeout: float = 30.0,
        argo_server_url: str = "https://argo-server.argo:2746",
        argo_server_insecure: bool = True,
    ):
        self._api_server = api_server
        self._token = token or self._read_sa_token()
        self._timeout = timeout
        # CRD client（submit/get_status/get_steps/cancel/stream_logs）—— 走 K8s API server。
        self._client = httpx.AsyncClient(
            base_url=api_server,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
            },
            verify=ssl.create_default_context(cafile=ca_cert_path),
            timeout=httpx.Timeout(connect=5.0, read=timeout, write=5.0, pool=5.0),
        )
        # argo-server client（resume）—— Argo CRD 不注册 resume 子资源，经 argo-server REST。
        # 始终发 SA token（D2）：server mode 下 argo-server 忽略调用方身份；client mode 下作身份。
        self._server_client = httpx.AsyncClient(
            base_url=argo_server_url,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
            },
            verify=not argo_server_insecure,
            timeout=httpx.Timeout(connect=5.0, read=timeout, write=5.0, pool=5.0),
        )
```

(c) `resume`（:295-304）改走 `_server_client`：

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

(d) `close`（:367-368）加 `_server_client`：

```python
    async def close(self) -> None:
        await self._server_client.aclose()
        await self._client.aclose()
```

- [ ] **Step 5: main.py argo_lifespan 加 init_kwargs**

`services/services/workflow/src/workflow_svc/main.py`（:24-29），k8s 分支 `init_kwargs` 追加两键：

```python
    if mode == "k8s":
        # K8sArgoClient 会自动读 SA token
        init_kwargs = {
            "api_server": getattr(settings, "k8s_api_server", "https://kubernetes.default.svc"),
            "argo_server_url": getattr(settings, "argo_server_url", "https://argo-server.argo:2746"),
            "argo_server_insecure": getattr(settings, "argo_server_insecure", True),
        }
```

- [ ] **Step 6: 跑测试确认通过**

Run: `/home/applo/project/ai-system-arch/.venv-t1/bin/pytest services/services/workflow/tests/ -q`
Expected: PASS —— 含新 `TestResumeViaArgoServer`（3）+ `test_init_no_verify_deprecation_warning`；既有 get_status/stream_logs 不回归。

- [ ] **Step 7: configmap（base + kind overlay）**

(a) `deploy/k8s/services/workflow/configmap.yaml`，`K8S_API_SERVER`（:25）后加：

```yaml
  K8S_API_SERVER: https://kubernetes.default.svc
  ARGO_SERVER_URL: https://argo-server.argo:2746
  ARGO_SERVER_INSECURE: "false"   # prod 默认安全；dev 由 kind overlay 翻 true
```

(b) `deploy/k8s/overlays/kind/patches/workflow-argo-mode.yaml`，`ARGO_MODE: k8s` 下加：

```yaml
data:
  ARGO_MODE: k8s
  ARGO_SERVER_INSECURE: "true"   # kind dev：argo-server 自签证书
```

- [ ] **Step 8: lint + 渲染校验**

Run:
```bash
cd /home/applo/project/ai-system-arch
.venv-t1/bin/ruff check services/services/workflow services/libs/apihub-core && .venv-t1/bin/ruff format --check services/services/workflow services/libs/apihub-core
.venv-t1/bin/mypy services/services/workflow/src
kustomize build deploy/k8s/overlays/kind >/dev/null && echo "kustomize OK"
```
Expected: ruff/mypy 全绿；渲染后 workflow-config 含 `ARGO_SERVER_INSECURE: "true"`（kind overlay 翻 true）。

- [ ] **Step 9: Commit**

```bash
git add services/libs/apihub-core/src/apihub_core/config.py \
  services/services/workflow/src/workflow_svc/argo_client.py \
  services/services/workflow/src/workflow_svc/main.py \
  deploy/k8s/services/workflow/configmap.yaml \
  deploy/k8s/overlays/kind/patches/workflow-argo-mode.yaml \
  services/services/workflow/tests/test_k8s_argo_client.py
git commit -m "feat(workflow): resume 走 argo-server + httpx verify ssl context 修复"
```

---

## Task 2: Phase 1 live smoke —— smoke C 硬化 + v3.0.3 resume e2e 硬绿

**Files:**
- Modify: `scripts/smoke/k8s-workflow-argo.py`（C 相软 warning → 硬断言）
- Verify (live kind): rebuild workflow image + rollout + 跑 smoke

**Interfaces:**
- Consumes: Task 1 的 `K8sArgoClient.resume`（走 argo-server）+ configmap `ARGO_SERVER_URL/INSECURE`。

- [ ] **Step 1: smoke C 硬化**

`scripts/smoke/k8s-workflow-argo.py`，C 相（:158-205）整段替换为（删 `c_warning` 软降分支）：

```python
    # ---- C) resume（硬断言：经 argo-server 真解除 suspend）----
    print("== C) submit suspend wf → resume ==")
    spec_c = {
        "serviceAccountName": "argo-exec",
        "entrypoint": "main",
        "templates": [
            {
                "name": "main",
                "steps": [
                    [{"name": "gate", "template": "hold"}],
                    [{"name": "fin", "template": "echo2"}],
                ],
            },
            {"name": "hold", "suspend": {}},
            {
                "name": "echo2",
                "container": {
                    "image": "busybox:latest",
                    "command": ["sh", "-c", "echo resumed"],
                    "imagePullPolicy": "IfNotPresent",
                },
            },
        ],
    }
    wf_c = submit(spec_c)
    poll(wf_c, {"running", "succeeded"}, timeout=60)
    st, raw = http("POST", f"{APISIX_URL}/v1/jobs/{wf_c}/resume", headers={"X-API-Key": DEMO_KEY})
    print(f"  POST /v1/jobs/{wf_c}/resume -> HTTP {st} {raw[:120]!r}")
    assert st == 200, f"C) resume HTTP {st}: {raw}"
    status_c, _, _ = poll(wf_c, {"succeeded", "failed"})
    print(f"  resume final status={status_c}")
    assert status_c == "succeeded", f"C) resume 后未 succeeded: {status_c}"
    print(f"WORKFLOW-C OK —— wf={wf_c} resume→{status_c}")
```

并把结尾汇总（:207-221）的 `c_warning` 分支删掉，D 相后直接：

```python
    print("ALL OK —— real Argo e2e green (resume via argo-server)")
    sys.exit(0)
```

（移除 `c_warning = ""` / `if c_warning:` / `ALL OK (with warning)` 残留。）

- [ ] **Step 2: rebuild workflow image + load + rollout**

```bash
cd /home/applo/project/ai-system-arch
make docker-build SERVICE=workflow
kind load docker-image registry.apihub.internal/apihub/workflow:0.1.0-dev --name apihub
# configmap 改了也 apply（单文件，不 raw 重 apply overlay）
kubectl --context kind-apihub apply -f deploy/k8s/services/workflow/configmap.yaml
kubectl --context kind-apihub -n apihub-system rollout restart deploy/workflow
kubectl --context kind-apihub -n apihub-system rollout status deploy/workflow --timeout=120s
```

- [ ] **Step 3: 跑 smoke（v3.0.3，A/B/C/D 全绿，C 现硬断言）**

Run: `cd /home/applo/project/ai-system-arch && env -u HTTPS_PROXY -u HTTP_PROXY -u https_proxy -u http_proxy -u ALL_PROXY -u all_proxy python3 scripts/smoke/k8s-workflow-argo.py`
Expected: exit 0；输出含 `WORKFLOW-C OK`（resume→succeeded）+ `ALL OK`。失败 → `kubectl --context kind-apihub -n apihub-system logs deploy/workflow` + `kubectl --context kind-apihub -n argo logs deploy/argo-server` 排查（resume 走 argo-server）。

- [ ] **Step 4: Commit**

```bash
git add scripts/smoke/k8s-workflow-argo.py
git commit -m "test(smoke): workflow resume C 相软 warning → 硬断言（v3.0.3 argo-server）"
```

---

## Task 3: Phase 2 re-pin Argo v3.5.15 + emissary + 全 smoke 回归

**Files:**
- Modify: `scripts/kind/argo-setup.sh`（ARGO_VERSION v3.5.15 + emissary + argo-server auth-mode + 复核 workaround）
- Modify: `scripts/smoke/k8s-workflow-minio.py`（删 produce step `&& sleep 2`）
- Verify (live kind): 重装 Argo v3.5.15 + 跑两个 smoke 全绿

**Interfaces:**
- Consumes: Task 1/2 的 resume-via-argo-server（v3.5 上仍走 argo-server，版本无关）。

- [ ] **Step 1: argo-setup.sh re-pin v3.5.15**

(a) 顶部版本（:18）+ 版本耦合注释（:43-45）：

```bash
ARGO_VERSION="${ARGO_VERSION:-v3.5.15}"
```

更新 :43-45 注释为：版本钉 v3.5.15（emissary 默认执行器）；re-pin 已弃用 pns/sleep-2 相关 workaround。升级时改顶部 `ARGO_VERSION` 并复核下方 emissary/auth-mode/双形态探测。

(b) artifactRepository ConfigMap 执行器（:125）`pns` → `emissary`：

```yaml
    config: |
      containerRuntimeExecutor: emissary
      artifactRepository:
```

(c) apply install.yaml + 等 controller 后（:94 后），加 argo-server 确定性 auth-mode（D7）：

```bash
# ---------- 3.5) argo-server auth mode 确定性（D7） ----------
# install.yaml 默认 auth-mode 版本间漂移；显式 --auth-mode server 使 resume 免鉴权稳。
# D2（始终发 SA token）使其 auth-mode 无关，此处为确定性双保险。
ARGO_SERVER_ARGS=$(kubectl -n "$ARGO_NS" get deploy argo-server \
  -o jsonpath='{.spec.template.spec.containers[0].args}' 2>/dev/null || true)
if ! printf '%s' "$ARGO_SERVER_ARGS" | grep -q 'server'; then
  log "patch argo-server --auth-mode server"
  kubectl -n "$ARGO_NS" patch deploy argo-server --type=strategic \
    -p='{"spec":{"template":{"spec":{"containers":[{"name":"argo-server","args":["--auth-mode","server"]}]}}}}'
  kubectl -n "$ARGO_NS" rollout status deploy/argo-server --timeout=120s
fi
```

- [ ] **Step 2: 复核 7 处 v3.0.3 workaround 在 v3.5.15（逐一跑 argo-setup 时验证）**

- 无引号 image 正则（:57）：v3.5 quoted image → 现有 regex `("[^"]+"|[^[:space:]]+)` 双形态，确认 `mapfile -t IMAGES` 非空。
- ns 显式 create + `-n argo` apply（:90-92）：保留。
- configmap 两参/等号双探测（:110-114）：确认 `CM_NAME` 非空。
- executor-image 双形态探测（:68-76）：确认 `EXEC_IMGS` 含 argoexec；若 v3.5 改 flag 名，按实际改 grep。
- no-scheme S3 endpoint（:39）：bare host:port 保留。
- argo-minio-secret 双 ns（:98-106）：emissary 仍需，保留。
- 自检（:142-150）：全过。

任一处挂 → 针对性改探测正则/逻辑，不回退版本（除非 smoke 全崩）。

- [ ] **Step 3: MinIO smoke 删 sleep 2**

`scripts/smoke/k8s-workflow-minio.py`，produce step 命令删 `&& sleep 2`（emissary 无 PNS 捕获竞争）。找到形如 `... && sleep 2` 的 command，改成无 sleep。

- [ ] **Step 4: 重装 Argo v3.5.15（重跑 argo-setup）**

```bash
cd /home/applo/project/ai-system-arch
bash scripts/kind/argo-setup.sh
# 确认：CRD + controller Available + argo-server Running + emissary 执行器
kubectl --context kind-apihub -n argo get deploy workflow-controller argo-server
CM=$(kubectl --context kind-apihub -n argo get deploy workflow-controller -o jsonpath='{.spec.template.spec.containers[0].args}' | sed 's/\[\|"//g;s/\]//g;s/,/\n/g' | awk '/configmap/{getline;print;exit}')
kubectl --context kind-apihub -n argo get cm "${CM:-workflow-controller-configmap}" -o yaml | grep containerRuntimeExecutor
# 期望：containerRuntimeExecutor: emissary
```

- [ ] **Step 5: 全 smoke 回归（v3.5.15）**

```bash
cd /home/applo/project/ai-system-arch
env -u HTTPS_PROXY -u HTTP_PROXY -u https_proxy -u http_proxy -u ALL_PROXY -u all_proxy python3 scripts/smoke/k8s-workflow-argo.py
env -u HTTPS_PROXY -u HTTP_PROXY -u https_proxy -u http_proxy -u ALL_PROXY -u all_proxy python3 scripts/smoke/k8s-workflow-minio.py
```
Expected: 两个 smoke 均 exit 0（A/B/C/D + MinIO artifact 往返全绿）。emissary 若报 RBAC（如 `pods/exec is forbidden`）→ 给 `deploy/k8s/base/argo/argo-exec-rbac.yaml` 的 Role 加 `pods/exec`，重 apply 单文件后重跑 smoke。

- [ ] **Step 6: Commit**

```bash
git add scripts/kind/argo-setup.sh scripts/smoke/k8s-workflow-minio.py
# 若 B.3 加了 pods/exec：
# git add deploy/k8s/base/argo/argo-exec-rbac.yaml
git commit -m "feat(argo): re-pin v3.5.15 + emissary 执行器 + 去 sleep 2（resume 仍走 argo-server）"
```

---

## 全 plan 出口标准（spec §出口标准）

- **Phase 1**：`pytest services/services/workflow/` 全绿（+resume 单测）；httpx DeprecationWarning 消；smoke C on v3.0.3 硬绿；ruff/mypy 绿。
- **Phase 2**：`k8s-workflow-argo.py`（A/B/C/D）+ `k8s-workflow-minio.py` 全绿 on v3.5.15；emissary 生效；无 sleep 2。

完成全部 task → final whole-branch review（opus）→ finishing-a-development-branch。

## Testing Strategy

- Task 1：单测（httpx MockTransport）—— CI 可跑，无集群。
- Task 2/3：live kind smoke 退出码即验收（与既有 k8s-*.py 一致，不新增 CI workflow）。
- 每 task 独立 commit；T2→T3 链式（需 live 集群 + 镜像 rebuild）。

## Risks（见 spec §风险与兜底）

- re-pin 打破 e2e → D4：Phase 1 resume 已先交付；Phase 2 翻车可 `git revert` argo-setup 改动回 v3.0.3。
- emissary RBAC → B.3 watch-item 加 `pods/exec`。
- v3.5.15 install.yaml 形态变化 → Step 2 逐一复核，smoke 回归兜底。
