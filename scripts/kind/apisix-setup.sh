#!/usr/bin/env bash
# APISIX 进数据面 (Task 9 / Stage 3a):
#   helm 装 APISIX+etcd（NodePort 固定 30080）+ 配 key-auth consumer + route → dispatcher
#
# 路由策略：直用 Task 8 已验证过的 L1 同步转发路径 /dispatch/*  → dispatcher.apihub-system:80
#   （dispatcher 入口 ANY /dispatch/{rest:path}，seed 的 smoke-sync API 命中）。
#   APISIX key-auth 校验 X-API-Key，header 原样透传给 dispatcher，dispatcher 自身 auth
#   用同一把 ak_test_a_demo001 也通过 → 端到端 200 {"ok":true,...}。
#
# 对 brief 的修正 / 增强：
#   1) NodePort 固定到 30080（对齐 kind extraPortMapping）：helm 值 + 兜底 patch。
#   2) 路由指向真实 dispatcher 端点 /dispatch/*（而非不存在的 /smoke/* + /health/ready）。
#   3) Admin key 运行时从 apisix ConfigMap 发现（chart 2.16.0 未暴露 admin_key 取值入口，
#      自定义值不生效，故读取实际生效的 key，避免硬编码依赖 chart 行为）。
#   4) key-auth 插件显式 header="X-API-Key"（默认 header 是 "apikey"，会让带 X-API-Key
#      的请求被判 "Missing API key"；同时 dispatcher 也认 X-API-Key，一把钥匙两头通）。
#   5) 镜像预装：kind 节点继承宿主机 localhost 代理，容器内不可达 → 无法直拉 docker.io。
#      故先在宿主机 docker pull，再 kind load 进节点（与栈内其它 12 个 pod 同款做法）。
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"

NS=apihub-ingress
DEMO_KEY="ak_test_a_demo001"          # tenant_a/app_trading（02-seed.sql）
LOCAL_ADMIN_PORT=19180
GATEWAY_NODEPORT=30080
CLUSTER_NAME="${KIND_CLUSTER_NAME:-apihub}"
[ -z "${INGRESS_SHARED_SECRET:-}" ] && INGRESS_SHARED_SECRET="ingress-shared-dev"

log() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
say() { printf '  %s\n' "$*"; }

# ---------------------------------------------------------------------------
# 1) 装 helm
# ---------------------------------------------------------------------------
log "install helm"
if command -v helm >/dev/null 2>&1; then
  say "helm present: $(helm version --short)"
else
  say "downloading helm v3.16.0 ..."
  curl -sSL https://get.helm.sh/helm-v3.16.0-linux-amd64.tar.gz \
    | tar -xz -C "$HOME/.local/bin" --strip-components=1 linux-amd64/helm
  say "installed: $(helm version --short)"
fi

helm repo add apisix https://charts.apiseven.com >/dev/null
helm repo update >/dev/null
say "apisix chart repo ready"

# ---------------------------------------------------------------------------
# 2) helm install APISIX + etcd（NodePort，单副本 etcd）
#    先不用 --wait：节点拉不到镜像会超时，但资源已创建；下面预装镜像后再起 pod。
#    NodePort 30080 在 helm 值里指定，安装后再核对 / 兜底 patch（修正 #1）。
# ---------------------------------------------------------------------------
log "helm install APISIX (gateway NodePort $GATEWAY_NODEPORT)"
cat >/tmp/apisix-kind-values.yaml <<EOF
gateway:
  type: NodePort
  service:
    type: NodePort
    http:
      nodePort: ${GATEWAY_NODEPORT}
dashboard:
  enabled: true
etcd:
  replicaCount: 1
  image:
    registry: docker.io
    repository: bitnamilegacy/etcd
    tag: "3.5.9"   # pin 3.5：apisix 3.17 与 etcd 3.6 不兼容（config_etcd 报 "no healthy endpoint"）；3.5 为 Phase 2 实证可用版本
  # ⚠️ chart v12（面向 etcd 3.6）默认 liveness 探针走 /livez，但镜像 pin 在 3.5.9 ——
  # etcd 3.5 无 /livez（返 404）→ liveness 必败 → CrashLoop（实测 restarts=31）。
  # 不能在 helm values 改 probe path（bitnami etcd subchart 未暴露该取值入口），用下方 §4b patch 修。
EOF

# 容忍首次安装可能因镜像拉取超时失败（资源仍会创建）
helm upgrade --install apisix apisix/apisix -n "${NS}" --create-namespace \
  -f /tmp/apisix-kind-values.yaml --timeout 5m || \
  say "(helm install did not fully wait — continuing to pre-load images)"

# 2a) 兜底：显式预装 pinned 镜像（§3 的 mapfile 依赖 pod 已创建，etcd sts 可能晚于
#     apisix pod 创建而被漏抓 → apisix-etcd-0 ErrImagePull）。宿主机已缓存，直接 kind load。
for _img in apache/apisix:3.17.0-ubuntu bitnamilegacy/etcd:3.5.9 busybox:1.28; do
  if docker image inspect "${_img}" >/dev/null 2>&1; then
    kind load docker-image "${_img}" --name "${CLUSTER_NAME}" >/dev/null 2>&1 \
      && say "explicit kind load ok: ${_img}" || say "WARN kind load failed: ${_img}"
  else
    say "WARN host missing image (需先 docker pull): ${_img}"
  fi
done

# ---------------------------------------------------------------------------
# 3) 预装镜像到 kind 节点（修正 #5：节点继承宿主机 localhost 代理，不可达 docker.io）
# ---------------------------------------------------------------------------
log "pre-load APISIX images into kind node"
LOADED_NEW=0
preload_one() {
  local img="$1"
  [ -z "$img" ] && return 0
  # 归一化：去掉 docker.io/ 与 library/ 前缀，得到 docker 本地 tag
  local short="${img#docker.io/}"
  short="${short#library/}"
  say "image: ${img}  (local tag: ${short})"
  if docker image inspect "${short}" >/dev/null 2>&1; then
    say "  already present on host"
  else
    say "  pulling on host ..."
    if docker pull "${short}" || docker pull "${img}"; then
      LOADED_NEW=1
    else
      say "  PULL FAILED: ${img}"; return 1
    fi
  fi
  kind load docker-image "${short}" --name "${CLUSTER_NAME}" >/dev/null 2>&1 \
    || say "  (kind load skipped/failed for ${short})"
}

# 遍历命名空间内每个 pod 的每个容器镜像，逐行输出（jsonpath \n 自带换行，不会粘连）
mapfile -t IMAGES < <(kubectl -n "${NS}" get pods -o \
  jsonpath='{range .items[*]}{range .spec.initContainers[*]}{.image}{"\n"}{end}{range .spec.containers[*]}{.image}{"\n"}{end}{end}' \
  2>/dev/null | grep -v '^$')
for img in "${IMAGES[@]}"; do preload_one "$img" || true; done

# 仅在镜像刚补 / 或 pod 处于坏态时才重启，避免幂等重跑时无谓重启
podstate=$(kubectl -n "${NS}" get pods --no-headers 2>/dev/null || true)
if [ "${LOADED_NEW}" = "1" ] || echo "${podstate}" | grep -qE 'ImagePullBackOff|ErrImagePull|Init:|0/1|Pending'; then
  say "restart pods to pick up loaded images (or recover from bad state)"
  kubectl -n "${NS}" delete pod --all --ignore-not-found >/dev/null 2>&1 || true
else
  say "pods already healthy; skipping restart"
fi

# ---------------------------------------------------------------------------
# 4) 兜底：确保 gateway Service 的 http 端口 nodePort == 30080（修正 #1）
# ---------------------------------------------------------------------------
log "ensure gateway nodePort == ${GATEWAY_NODEPORT}"
pname=$(kubectl -n "${NS}" get svc apisix-gateway \
  -o jsonpath='{.spec.ports[?(@.port==80)].name}' 2>/dev/null || true)
[ -z "${pname}" ] && pname=$(kubectl -n "${NS}" get svc apisix-gateway \
  -o jsonpath='{.spec.ports[0].name}' 2>/dev/null || true)
[ -z "${pname}" ] && pname="http"
cur_np=$(kubectl -n "${NS}" get svc apisix-gateway \
  -o jsonpath="{.spec.ports[?(@.name=='${pname}')].nodePort}" 2>/dev/null || true)
if [ "${cur_np}" != "${GATEWAY_NODEPORT}" ]; then
  say "current nodePort=${cur_np:-<none>} (${pname}); patching -> ${GATEWAY_NODEPORT}"
  # JSON patch 改第一个 port 的 nodePort（strategic-merge list patch 曾报 "unexpected end of JSON input"）
  kubectl -n "${NS}" patch svc apisix-gateway --type=json \
    -p "[{\"op\":\"replace\",\"path\":\"/spec/ports/0/nodePort\",\"value\":${GATEWAY_NODEPORT}}]"
else
  say "nodePort already ${GATEWAY_NODEPORT}"
fi
say "apisix-gateway service:"
kubectl -n "${NS}" get svc apisix-gateway

# ---------------------------------------------------------------------------
# 4b) etcd liveness 探针路径修正（根因修复）：bitnami etcd chart v12 面向 etcd 3.6，
#     默认 liveness httpGet /livez；但 image pin 在 3.5.9（apisix 兼容）—— etcd 3.5
#     没有 /livez 端点（实测返 404），/health 才是 3.5 的健康端点（返 200）。
#     后果：liveness 永远 404 失败 → kubelet 杀 → CrashLoop（实测 restarts=31）
#     → APISIX 读不到配置 "no healthy etcd endpoint" → reset 所有连接（L5 必挂）。
#     chart 未暴露 probe path 取值，只能 post-install strategic patch 改 path。
#     幂等；改 pod template 触发 etcd 滚动重启 → 下方 §5 wait 接管。
# ---------------------------------------------------------------------------
log "fix etcd liveness probe path: /livez -> /health (image 3.5.9 无 /livez 端点)"
if kubectl -n "${NS}" get sts apisix-etcd >/dev/null 2>&1; then
  kubectl -n "${NS}" patch sts apisix-etcd --type=strategic \
    -p '{"spec":{"template":{"spec":{"containers":[{"name":"etcd","livenessProbe":{"httpGet":{"path":"/health"}}}]}}}}' \
    >/dev/null 2>&1 && say "etcd liveness path -> /health (根因修复；etcd 3.5 只有 /health)" \
    || say "WARN: etcd liveness path patch failed（不改则 etcd 必 CrashLoop）"
else
  say "(apisix-etcd sts not found yet; skip probe patch)"
fi

# ---------------------------------------------------------------------------
# 5) 等 APISIX / etcd pod 全部 Ready
# ---------------------------------------------------------------------------
log "wait for APISIX + etcd pods Ready"
# 轮询直到 apisix-* 与 apisix-etcd-0 都 Running 1/1（按 pod 名前缀匹配，最稳）
deadline=$(( $(date +%s) + 240 ))
while :; do
  ps=$(kubectl -n "${NS}" get pods --no-headers 2>/dev/null || true)
  apisix_ok=$(printf '%s\n' "${ps}" | awk '$1 ~ /^apisix-[a-f0-9]+-/ && $2=="1/1" && $3=="Running" {print "y"}')
  etcd_ok=$(printf '%s\n' "${ps}" | awk '$1 == "apisix-etcd-0" && $2=="1/1" && $3=="Running" {print "y"}')
  [ -n "${apisix_ok}" ] && [ -n "${etcd_ok}" ] && break
  if [ "$(date +%s)" -gt "${deadline}" ]; then
    echo "ERROR: APISIX/etcd pods not Ready within 240s" >&2
    kubectl -n "${NS}" get pods -o wide >&2 || true
    exit 1
  fi
  sleep 5
done
say "pods:"
kubectl -n "${NS}" get pods -o wide

say "data-plane probe (expect any APISIX response, e.g. 404):"
curl -s -o /dev/null -w "  curl http://127.0.0.1:${GATEWAY_NODEPORT} -> HTTP %{http_code}\n" \
  --max-time 10 "http://127.0.0.1:${GATEWAY_NODEPORT}/" || true

# ---------------------------------------------------------------------------
# 6) 通过 port-forward 拿到 Admin API；admin key 从 ConfigMap 发现（修正 #3）
# ---------------------------------------------------------------------------
log "configure consumer + key-auth + route via Admin API"

# 发现实际生效的 admin key（chart 2.16.0 不接受自定义 admin_key 取值）
ADMIN_KEY=$(kubectl -n "${NS}" get cm apisix -o jsonpath="{.data['config\.yaml']}" 2>/dev/null \
  | awk '/name: "admin"/{found=1} found && /key:/ {print $2; exit}')
[ -z "${ADMIN_KEY}" ] && ADMIN_KEY="edd1c9f034335f136f87ad84b625c8f1"   # APISIX 默认 admin key 兜底
say "discovered admin key: ${ADMIN_KEY}"

admin_svc=$(kubectl -n "${NS}" get svc -l app.kubernetes.io/component=admin \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
[ -z "${admin_svc}" ] && admin_svc="apisix-admin"
admin_port=$(kubectl -n "${NS}" get svc "${admin_svc}" \
  -o jsonpath='{.spec.ports[0].port}' 2>/dev/null || echo 9180)
say "admin service=${admin_svc} port=${admin_port}"

kubectl -n "${NS}" port-forward "svc/${admin_svc}" \
  "${LOCAL_ADMIN_PORT}:${admin_port}" >/tmp/apisix-admin-pf.log 2>&1 &
PF_PID=$!
trap 'kill ${PF_PID} 2>/dev/null || true' EXIT
say "port-forward pid=${PF_PID}; waiting for admin API ..."
ADMIN_UP=0
for _ in $(seq 1 40); do
  if curl -s -o /dev/null --max-time 2 \
       -H "X-API-KEY: ${ADMIN_KEY}" "http://127.0.0.1:${LOCAL_ADMIN_PORT}/apisix/admin/consumers"; then
    ADMIN_UP=1; break
  fi
  sleep 0.5
done
if [ "${ADMIN_UP}" != "1" ]; then
  echo "ERROR: admin API port-forward did not come up" >&2
  cat /tmp/apisix-admin-pf.log >&2 || true
  exit 1
fi
say "admin API reachable"

ADMIN="http://127.0.0.1:${LOCAL_ADMIN_PORT}/apisix/admin"

# 6a) consumer：smoke / key-auth key = ak_test_a_demo001
#     R3b S1-T3：consumer 携带 labels.home_region="sh" —— 演示 tenant-affinity 写亲和
#     （APISIX 插件读 labels.home_region 决定写路由 region）。
say "upsert consumer 'smoke' (key-auth key=${DEMO_KEY}, home_region=sh)"
curl -s "${ADMIN}/consumers/smoke" -H "X-API-KEY: ${ADMIN_KEY}" -X PUT \
  -d "{\"username\":\"smoke\",\"plugins\":{\"key-auth\":{\"key\":\"${DEMO_KEY}\"}},\"labels\":{\"home_region\":\"sh\"}}" \
  -o /dev/null -w "  consumer PUT -> %{http_code}\n"

# 6b) route：/dispatch/* → dispatcher.apihub-system:80，key-auth 读 X-API-Key（修正 #4）。
#     R1c 后 dispatcher /dispatch 强制要 X-API-Version-Id（否则 400）—— 这条 smoke 路由
#     用 proxy-rewrite 注入 seed 的 smoke 版本 ver_smoke_sync_v1（published），让 §7 的
#     good-key 调用能走通 APISIX key-auth → 注入 header → dispatcher resolve → mock-backend。
#     R1d：同时注入 X-Ingress-Auth=<INGRESS_SHARED_SECRET> —— dispatcher 信任入口快路径
#     （dispatcher 走 trusted-ingress fast path，跳过自身 API-key/identity 校验）。
say "upsert route 'dispatcher' (/dispatch/* -> dispatcher.apihub-system:80, inject X-API-Version-Id + X-Ingress-Auth)"
curl -s "${ADMIN}/routes/dispatcher" -H "X-API-KEY: ${ADMIN_KEY}" -X PUT \
  -d '{"uri":"/dispatch/*","upstream":{"type":"roundrobin","nodes":{"dispatcher.apihub-system:80":1}},"plugins":{"key-auth":{"header":"X-API-Key"},"proxy-rewrite":{"headers":{"set":{"X-API-Version-Id":"ver_smoke_sync_v1","X-Ingress-Auth":"'"${INGRESS_SHARED_SECRET}"'"}}}}}' \
  -o /dev/null -w "  route dispatcher PUT -> %{http_code}\n"

# 6c) route：/v1/jobs → dispatcher.apihub-system:80（workflow 入口，key-auth 同 /dispatch/*）
# uris 数组同时覆盖 POST /v1/jobs（精确）与 GET /v1/jobs/{id}（通配），
# 单写 /v1/jobs/* 不匹配无尾段的 POST /v1/jobs。
say "upsert route 'jobs' (/v1/jobs, /v1/jobs/* -> dispatcher.apihub-system:80)"
curl -s "${ADMIN}/routes/jobs" -H "X-API-KEY: ${ADMIN_KEY}" -X PUT \
  -d '{"uris":["/v1/jobs","/v1/jobs/*"],"upstream":{"type":"roundrobin","nodes":{"dispatcher.apihub-system:80":1}},"plugins":{"key-auth":{"header":"X-API-Key"}}}' \
  -o /dev/null -w "  route jobs PUT -> %{http_code}\n"

# 关掉 admin port-forward（配置完成）
kill "${PF_PID}" 2>/dev/null || true
trap - EXIT

# ---------------------------------------------------------------------------
# 7) 端到端验证：经 APISIX key-auth → dispatcher → mock-backend
# ---------------------------------------------------------------------------
log "end-to-end: curl through APISIX -> dispatcher"
say "1) no key (expect 401, key-auth rejects):"
nokey=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 \
  -X POST "http://127.0.0.1:${GATEWAY_NODEPORT}/dispatch/smoke-sync/echo" \
  -H "Content-Type: application/json" -d '{"hello":"world"}')
say "  no-key  POST /dispatch/smoke-sync/echo -> HTTP ${nokey}"

say "2) wrong key (expect 401):"
badkey=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 \
  -X POST "http://127.0.0.1:${GATEWAY_NODEPORT}/dispatch/smoke-sync/echo" \
  -H "X-API-Key: wrong-key" -H "Content-Type: application/json" -d '{"hello":"world"}')
say "  bad-key POST /dispatch/smoke-sync/echo -> HTTP ${badkey}"

say "3) valid key (expect 200 {\"ok\":true,...} from mock-backend echo):"
code=$(curl -s -o /tmp/apisix-resp.json -w "%{http_code}" --max-time 15 \
  -X POST "http://127.0.0.1:${GATEWAY_NODEPORT}/dispatch/smoke-sync/echo" \
  -H "X-API-Key: ${DEMO_KEY}" -H "Content-Type: application/json" \
  -d '{"hello":"world"}')
say "  good-key POST /dispatch/smoke-sync/echo -> HTTP ${code}"
say "  response body: $(cat /tmp/apisix-resp.json)"

if [ "${code}" = "200" ] && [ "${nokey}" = "401" ]; then
  log "SUCCESS: APISIX in data path — key-auth gate + dispatcher 200"
else
  log "WARNING: expected (good=200, nokey=401); got (good=${code}, nokey=${nokey})"
  log "  401=auth mismatch, 502/503=upstream, 404=path/route missing"
fi
