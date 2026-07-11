# Handoff — k8s-links 5/5 GREEN 硬化（#2 startupProbe / #3 verify+etcd+nodePort / #4 argocd）

> 批量生产硬化一轮（#2/#3/#4），打一个 squash-PR。续点 #1（开 PR/合并）已由 #14 完成。
> **目标达成**：fresh deploy/restart 后 `scripts/smoke/k8s-links.py` **2× 连续冷启动 ALL 5 LINKS GREEN**（修复前 2/5）。

## 成果

- 冷启动 smoke **2/5 → 5/5**（L1/L4 Auth unreachable、L5 reset 全部消除）。
- 顺带挖出并修了**两个 severity 高的真 bug**（etcd CrashLoop、apisix nodePort drift），handoff/my 初判都曾被误导。

## 真根因（systematic-debugging 实跑取证；handoff 旧报告的几处前提被推翻）

1. **#2 startupProbe**：10/11 服务无 startupProbe（只 auth 有，注释"曾 RESTARTS 13"）。→ 给 10 个 base deployment 补 auth 同款（`/health/ready`, period 5s, failureThreshold 24 = 120s 窗口）。**#2 的 120s 窗口同时兜住 #3a 的 pool 预热建连耗时**（协同）。
2. **#3a L1/L4 "Auth service unreachable"**：auth PG pool **冷启动**（asyncpg `min_size=10` 但惰性建连，#14 重启后 pool 冷）+ dispatcher `httpx.AsyncClient(timeout=2.0)` 太紧；smoke 的 `redis_del` **必触** cache-miss → 走冷 PG 建连慢路径（实测 3.3s ~ >15s）→ 撞 2.0s → 503。错误明细**空串**（httpx 超时类异常 str 常空）让定位绕了大弯。pool 热=7ms。
   → `db.init_pool` 启动预热 min_size 连接（并发持有再释放）+ `auth.py` timeout 2→5s + 错误带 `type(e).__name__`/`repr`。
3. **#3b L5 reset —— 两个独立根因**：
   - **etcd CrashLoop（实测 restarts=31）**：bitnami etcd chart v12 面向 etcd **3.6**，默认 liveness `/livez`；但 image pin 在 **3.5.9**（apisix 3.17 与 3.6 不兼容）—— etcd 3.5 **没有 `/livez` 端点（返 404）**，`/health` 才是 3.5 的健康端点（200）。liveness 永远 404 → kubelet 杀 → CrashLoop → APISIX `no healthy etcd endpoint` reset 所有连接。⚠️ **"资源/CPU throttle"是红鲱鱼**（实测 `etcdctl health` 2.4ms，375m 够用；restarts 全是 404 探针杀的）。fix：`apisix-setup.sh` post-install strategic patch liveness `/livez`→`/health`。
   - **apisix-gateway nodePort drift**：Service nodePort 跑成 30367（chart default），但 kind extraPortMapping + smoke 期望 **30080** → host:30080 reset（无 Service 监听 30080）。脚本 §4 patch 过 30080，但 helm 会 revert（live 已发生）。fix：live patch 30080（脚本 §4 fresh install 会重设）。
4. **#4 argocd**：repo-server `HTTPS_PROXY` + `argocd-cm timeout.reconciliation=30s`（live 已 patch，未 commit 进脚本）。→ `argocd-setup.sh` 补这两个 patch（buildOptions patch 之后，一并 restart）。

### 额外修：auth 启动 CrashLoop（Kafka bootstrap）
auth 镜像 `--workers 2` → 启动 CrashLoop：aiokafka producer 在 **fork 子进程** bootstrap Kafka，遇 `host.docker.internal` 抖动必败 → "Application startup failed" 进程 exit → startupProbe 救不了（process 死，不是慢）→ CrashLoop（曾 RESTARTS 13，handoff 已知）。dispatcher `--workers 4` 不受影响（稳定）。→ auth Dockerfile `--workers 2→1`（auth 低 QPS，HA 走 replicas）。

## 核心教训（高价值）

1. **chart-version vs image-version 探针错配**：chart 按某镜像版本配探针（`/livez` for etcd 3.6），但 image pin 旧版（3.5.9 无 `/livez`）→ 404 → CrashLoop。**pin 镜像版本时必须核对探针端点是否存在于该版本**。
2. **HTTP curl 测非 HTTP 端口 = 红鲱鱼**：curl PG/Kafka/Redis 端口返 000（协议不匹配），误判"不可达"。**TCP 级测试**（perl `IO::Socket::INET`）才准。
3. **错误日志别丢异常类型**：httpx 超时类异常 str 常空，`f"...: {e}"` 只剩 "unreachable: "。用 `{type(e).__name__}: {e!r}`。
4. **GitOps 之外的组件**（APISIX/etcd/ArgoCD 装脚本）**不被 ArgoCD 管**，helm 会 revert 运行时 patch（nodePort）—— fresh install 脚本里必须 idempotent 重设；live 集群需手动 recover。
5. **错误前提要复核**：handoff 说"L4 过"实测错（L4 也挂）、"L1 经 APISIX"实测 L1 是直连 dispatcher。"已绿"的 L5 从未真绿。复现 > 信任旧报告。

## 文件变更（14 files, +117/-3）

- 10 × `deploy/k8s/services/{admin,api-registry,dispatcher,docs,executor,quota,retry,tenant,trace,workflow}/deployment.yaml`（+startupProbe，镜像 auth 的块）
- `services/libs/apihub-core/src/apihub_core/db.py`（init_pool 启动预热 min_size 连接）
- `services/libs/apihub-core/src/apihub_core/auth.py`（timeout 2→5s + 错误带异常类型/repr）
- `scripts/kind/apisix-setup.sh`（etcd liveness `/livez`→`/health` 根因修复 + 注释说明）
- `scripts/k8s/argocd-setup.sh`（+repo-server HTTPS_PROXY/NO_PROXY + argocd-cm timeout.reconciliation=30s）
- `services/services/auth/Dockerfile`（`--workers 2→1`）

## 验证

- **2× 连续冷启动** `python3 scripts/smoke/k8s-links.py` = `ALL 5 LINKS GREEN`（auth/dispatcher/admin freshly rebuilt + restarted，无手动焐热 pool）。
- etcd `ready=true restarts=0`（was 31）；APISIX etcd errors=0；auth/dispatcher/admin `ready=true restarts=0`。
- `ruff check` clean；apihub-core 单测通过（**1 个 pre-existing #14 stale test 见残留 #1**）。
- #4：`argocd-cm timeout.reconciliation=30s` + repo-server HTTPS_PROXY live 在场 = 脚本一致。

## 残留 / followup

1. **stale test（pre-existing #14，非本轮引入）**：`test_config.py::test_executor_port_env_override` 设 `EXECUTOR_PORT`，但 #14 `d62514c` 改 `executor_port` 用 `validation_alias="EXECUTOR_APP_PORT"`（避 k8s `<SVC>_PORT` 注入）→ 测试 stale fail。建议改测试为 `EXECUTOR_APP_PORT`（1 行）。`git show HEAD:` 已证明二者矛盾。
2. **nodePort drift 风险**：helm 会把 apisix-gateway nodePort revert 到 30367；脚本 §4 idempotent 重设，但**非脚本触发的 helm 操作会 revert**。本轮用户选"只记 handoff"，未加额外守卫；如复发可在 `apisix-setup.sh` 末尾加 nodePort 断言。
3. **CH 容器 unhealthy**（auth 连 CH 成功 `clickhouse_initialized`，非阻断）—— handoff 已知 issue。
4. **>15s 病态 PG 冷建连**（dev 宿主网络/CoreDNS）：startupProbe + prewarm 大幅缓解，非纯 app 可根治。
5. **完整 fleet 镜像刷新**：本轮只 rebuild 了 auth/dispatcher/admin（smoke 路径）。其余 8 服务仍跑旧 apihub-core（无 prewarm）—— L2/L3 不受影响（已 green），但为一致性建议后续 rebuild 全 fleet（db.py 改动受益所有服务的 pool）。

## 部署/环境

- kind cluster name = **`apihub`**（context 是 `kind-apihub`；`kind load` 必须 `--name apihub`，不是 `kind-apihub` —— 本轮踩过）。
- 本轮 live-patched（恢复用）：etcd liveness→`/health`、apisix-gateway nodePort→30080、re-applied APISIX routes（CrashLoop 期 etcd 数据丢失）、rebuilt+reloaded auth/dispatcher/admin、auth 1-worker。
- **待开 PR**：分支 `feat/k8s-links-hardening`（本地 commit 待提交，**push/merge 等发话**）。
