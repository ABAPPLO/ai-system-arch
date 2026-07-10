#!/usr/bin/env bash
# =============================================================================
# Argo Workflow 安装（kind/dev）：
#   1) curl --noproxy 拉官方 install.yaml（绕 host 代理坑）
#   2) 镜像 host pull → kind load（kind 节点继承 host 代理，容器内拉不到）
#   3) kubectl apply + 等 controller ready
#   4) 配 artifactRepository → MinIO（host 网桥 IP:port）
#   5) 自检
#
# 前提：kind 集群 kind-apihub 在；apihub-minio 容器在跑（bootstrap Task 5）；
#       argo-exec SA 已 apply（deploy overlay Task 3）。
# =============================================================================
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"

CLUSTER_NAME="${KIND_CLUSTER_NAME:-apihub}"
ARGO_NS=argo
ARGO_VERSION="${ARGO_VERSION:-v3.5.15}"
WF_NS=apihub-workflow
MINIO_CONTAINER=apihub-minio
MINIO_USER="${MINIO_USER:-apihub}"
MINIO_PASSWORD="${MINIO_PASSWORD:-apihub_dev_pwd}"
ARGO_BUCKET=argo-artifacts

log() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
say() { printf '  %s\n' "$*"; }

# ---------- 0) 探测 host IP + MinIO 端口（kind pod 经此访问 host MinIO）----------
HOST_IP=$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}')
MINIO_HP=$(docker port "$MINIO_CONTAINER" 9000 2>/dev/null | awk -F: '/^0\.0\.0.0:/ {print $NF; exit}')
if [ -z "${MINIO_HP:-}" ]; then
  echo "FATAL: apihub-minio 未起或未 publish 9000（先跑 bootstrap Task 5 起 MinIO）" >&2
  exit 1
fi
MINIO_EP="http://${HOST_IP}:${MINIO_HP}"
# minio-go S3 client：v3.0.3 实证不接受 endpoint 带_scheme_（http://…）——报 "too many colons
# in address"（把 http: 的冒号也算进去），须裸 host:port + insecure:true 走 HTTP。v3.5/emissary
# 的 minio-go 较新，可能已接受 scheme（watch-item：smoke artifact 失败再放开）。先沿用裸 endpoint。
MINIO_EP_NOSCHEME="${HOST_IP}:${MINIO_HP}"
say "host_ip=$HOST_IP minio_endpoint=$MINIO_EP (s3-endpoint=$MINIO_EP_NOSCHEME)"

# ---------- 1) 拉 install.yaml（绕代理）----------
# v3.5.x 起 raw manifests/install.yaml 路径被移除（404，重组为 base/cluster-install/
# namespace-install/ kustomize + quick-start-*.yaml 单文件）→ 改拉 GitHub release asset：
# 单文件、镜像版本-pin（argocli/workflow-controller:ARGO_VERSION，非 :latest）、无 bundled minio、
# 含全部 CRD（含 workflows.argoproj.io）。v3.0.3 release 同样提供该 asset，故回滚
# (ARGO_VERSION=v3.0.3) 走同一 URL。argo-server args=[server]（auth-mode server，无需 patch）；
# controller args=[]（无 --configmap/--executor-image，见下 §2 executor 兜底 + §4 configmap 默认名）。
log "fetch argo-workflows install.yaml ($ARGO_VERSION) from release assets"
INSTALL=/tmp/argo-install.yaml
URL="https://github.com/argoproj/argo-workflows/releases/download/${ARGO_VERSION}/install.yaml"
# github release 下载经 objects.githubusercontent.com，host 直连 flaky（TLS handshake timeout）；
# 镜像已预载时这是唯一网络依赖。先直连（--noproxy），失败回退环境代理（HTTPS_PROXY）兜底。
if ! env -u HTTPS_PROXY -u HTTP_PROXY -u https_proxy -u http_proxy -u ALL_PROXY -u all_proxy \
     curl --noproxy '*' -fsSL --retry 3 --retry-delay 2 --max-time 60 "$URL" -o "$INSTALL"; then
  say "direct fetch failed → retry via environment proxy (HTTPS_PROXY)"
  curl -fsSL --retry 3 --retry-delay 2 --max-time 120 "$URL" -o "$INSTALL"
fi
say "fetched $(grep -c '^kind:' "$INSTALL") manifests"

# ---------- 2) 镜像预拉 → kind load ----------
log "preload argo images (host pull → kind load)"
# 注：当前 Argo install.yaml（v3.0.3）用无引号 image:（v3.0.x）；旧版用双引号。两者兼容。
mapfile -t IMAGES < <(grep -oE 'image:[[:space:]]*("[^"]+"|[^[:space:]]+)' "$INSTALL" \
  | sed -E 's/image:[[:space:]]*//; s/^"//; s/"$//' | sort -u)
for img in "${IMAGES[@]}"; do
  say "pull+load $img"
  docker image inspect "$img" >/dev/null 2>&1 || docker pull "$img"
  kind load docker-image "$img" --name "$CLUSTER_NAME"
done
# argoexec 镜像不在 install.yaml 的 image: 字段（v3.0.x 经 controller --executor-image 传入；
# v3.5 controller args=[] 用默认 executor）。上面 image: 抓取漏掉它 → 不预载会 ImagePullBackOff。
# 先探测 --executor-image flag（v3.0.x 两形态），落空则兜底推导 argoexec（见下 v3.5 分支）：
#   两参（独立 YAML 列表项）：`- --executor-image` 下一行 `- IMG`
#   等号：`--executor-image=IMG`
mapfile -t EXEC_IMGS < <(
  {
    grep -A1 '^[[:space:]]*- --executor-image[[:space:]]*$' "$INSTALL" \
      | grep -oE '^[[:space:]]*- ("[^"]+"|[^[:space:]]+)' \
      | sed -E 's/^[[:space:]]*- //; s/^"//; s/"$//'
    grep -oE 'executor-image=("[^"]+"|[^[:space:]]+)' "$INSTALL" \
      | sed -E 's/executor-image=//; s/^"//; s/"$//'
  } | grep -vE '^--' | sort -u
)
# v3.5 controller args=[]（无 --executor-image flag）→ 探测落空，用默认 executor 镜像：
# 与 workflow-controller 同 repo 同 tag 的 argoexec（quay.io/argoproj/argoexec:ARGO_VERSION）。
if [ "${#EXEC_IMGS[@]}" -eq 0 ]; then
  CTRL_IMG=$(grep -oE 'image:[[:space:]]*("[^"]+"|[^[:space:]]+)' "$INSTALL" \
    | sed -E 's/image:[[:space:]]*//; s/^"//; s/"$//' | grep 'workflow-controller' | head -n1)
  if [ -n "$CTRL_IMG" ]; then
    EXEC_IMGS+=("${CTRL_IMG%/*}/argoexec:${CTRL_IMG##*:}")
  else
    EXEC_IMGS+=("quay.io/argoproj/argoexec:${ARGO_VERSION}")
  fi
  say "v3.5 controller args empty → inferred executor ${EXEC_IMGS[*]}"
fi
for img in "${EXEC_IMGS[@]}"; do
  say "pull+load executor $img"
  docker image inspect "$img" >/dev/null 2>&1 || docker pull "$img"
  kind load docker-image "$img" --name "$CLUSTER_NAME"
done
# smoke step 镜像
say "pull+load busybox:latest"
docker image inspect busybox:latest >/dev/null 2>&1 || docker pull busybox:latest
kind load docker-image busybox:latest --name "$CLUSTER_NAME"

# ---------- 3) apply + 等 controller ----------
# Argo install.yaml（v3.0.x/v3.5.x release asset 均如此）不带 Namespace 定义，且其中
# namespaced 资源未写死 namespace: argo → 必须显式创建 argo ns 并用 -n argo apply（否则落到 default）。
kubectl create namespace "$ARGO_NS" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
log "kubectl apply argo install.yaml"
kubectl apply -n "$ARGO_NS" -f "$INSTALL"
log "wait workflow-controller Available"
kubectl -n "$ARGO_NS" wait deploy/workflow-controller --for=condition=Available --timeout=300s

# ---------- 4) artifactRepository → MinIO ----------
log "configure artifactRepository → MinIO ($ARGO_BUCKET)"
kubectl -n "$ARGO_NS" create secret generic argo-minio-secret \
  --from-literal=accessKey="$MINIO_USER" --from-literal=secretKey="$MINIO_PASSWORD" \
  -o yaml --dry-run=client | kubectl apply -f -
# 同名 secret 还须存在于 workflow 执行 ns（WF_NS）——argoexec 在 wf pod 内以 Volume 形式
# mount 该 secret 取 S3 凭证（pns/emissary 均如此）；pod 跑在 WF_NS，只认本 ns 的 secret，否则
# MountVolume.SetUp 报 "secret argo-minio-secret not found"，wf 卡 ContainerCreating。
kubectl -n "$WF_NS" create secret generic argo-minio-secret \
  --from-literal=accessKey="$MINIO_USER" --from-literal=secretKey="$MINIO_PASSWORD" \
  -o yaml --dry-run=client | kubectl apply -f -

# 探测 controller 实际读的 ConfigMap 名（版本间命名漂移：configmap vs config-map；
# v3.0.x 用 "--configmap NAME" 两参形式，旧版用 --configmap=NAME，两者兼容）。
CM_NAME=$(kubectl -n "$ARGO_NS" get deploy workflow-controller \
  -o jsonpath='{.spec.template.spec.containers[0].args}' 2>/dev/null \
  | sed 's/\[\|"//g; s/\]//g; s/,/\n/g' \
  | awk '/^--configmap=/ {sub(/^--configmap=/,""); print; exit} /^--configmap$/ {getline; print; exit}')
CM_NAME="${CM_NAME:-workflow-controller-configmap}"
say "controller configmap = $CM_NAME"

cat <<EOF | kubectl -n "$ARGO_NS" apply -f -
apiVersion: v1
kind: ConfigMap
metadata:
  name: ${CM_NAME}
  namespace: ${ARGO_NS}
data:
  config: |
    # executor 不显式设置：v3.5 起默认 emissary（v3.3+ 默认）；且旧 string 字段
    # `containerRuntimeExecutor` 被 v3.5 严格 parser 拒绝（unknown field → controller fatal）。
    # controller 自动用同 tag 的 argoexec（quay.io/argoproj/argoexec:ARGO_VERSION，见 §2 预载）。
    artifactRepository:
      s3:
        endpoint: "${MINIO_EP_NOSCHEME}"
        bucket: ${ARGO_BUCKET}
        insecure: true
        accessKeySecret:
          name: argo-minio-secret
          key: accessKey
        secretKeySecret:
          name: argo-minio-secret
          key: secretKey
EOF
# 重启 controller 让其重读 config
kubectl -n "$ARGO_NS" rollout restart deploy/workflow-controller
kubectl -n "$ARGO_NS" rollout status deploy/workflow-controller --timeout=180s

# ---------- 5) 自检 ----------
log "self-check"
kubectl get crd workflows.argoproj.io >/dev/null
kubectl -n "$ARGO_NS" get deploy workflow-controller >/dev/null
kubectl -n "$WF_NS" get sa argo-exec >/dev/null
kubectl -n "$ARGO_NS" get cm "$CM_NAME" >/dev/null
kubectl -n "$ARGO_NS" get secret argo-minio-secret >/dev/null
kubectl -n "$WF_NS" get secret argo-minio-secret >/dev/null
say "ARGO SETUP OK —— controller ready, argo-exec SA present, artifactRepository→MinIO"
