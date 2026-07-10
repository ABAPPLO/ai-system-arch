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
# Argo v3.0.3 的 minio-go S3 client 不接受 endpoint 带_scheme_（http://…）——
# 报 "too many colons in address"（把 http: 的冒号也算进去）。endpoint 必须是裸 host:port，
# 由 insecure:true 决定走 HTTP。详见 argoexec wait 日志。
MINIO_EP_NOSCHEME="${HOST_IP}:${MINIO_HP}"
say "host_ip=$HOST_IP minio_endpoint=$MINIO_EP (s3-endpoint=$MINIO_EP_NOSCHEME)"

# ---------- 1) 拉 install.yaml（绕代理）----------
log "fetch argo-workflows install.yaml (stable)"
INSTALL=/tmp/argo-install.yaml
env -u HTTPS_PROXY -u HTTP_PROXY -u https_proxy -u http_proxy -u ALL_PROXY -u all_proxy \
  curl --noproxy '*' -fsSL \
  https://raw.githubusercontent.com/argoproj/argo-workflows/stable/manifests/install.yaml \
  -o "$INSTALL"
say "fetched $(grep -c '^kind:' "$INSTALL") manifests"

# ---------- 2) 镜像预拉 → kind load ----------
log "preload argo images (host pull → kind load)"
# 注：当前 Argo stable install.yaml 用无引号 image:（v3.0.x）；旧版用双引号。两者兼容。
mapfile -t IMAGES < <(grep -oE 'image:[[:space:]]*("[^"]+"|[^[:space:]]+)' "$INSTALL" \
  | sed -E 's/image:[[:space:]]*//; s/^"//; s/"$//' | sort -u)
for img in "${IMAGES[@]}"; do
  say "pull+load $img"
  docker image inspect "$img" >/dev/null 2>&1 || docker pull "$img"
  kind load docker-image "$img" --name "$CLUSTER_NAME"
done
# argoexec 镜像经 controller 的 --executor-image 标志传入（NOT install.yaml 的 image: 字段），
# 故上面 image: 抓取漏掉它 → 不预载会 ImagePullBackOff。两种 flag 形态都覆盖：
#   两参（v3.0.x，独立 YAML 列表项）：`- --executor-image` 下一行 `- IMG`
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
# Argo v3.0.x install.yaml 不再自带 Namespace 定义（旧版有），且其中 namespaced 资源
# 未写死 namespace: argo → 必须显式创建 argo ns 并用 -n argo apply（否则落到 default）。
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
# 同名 secret 还须存在于 workflow 执行 ns（WF_NS）——argoexec(pns) 在 wf pod 内以
# Volume 形式 mount 该 secret 取 S3 凭证；pod 跑在 WF_NS，只认本 ns 的 secret，否则
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
    containerRuntimeExecutor: pns
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
say "ARGO SETUP OK —— controller ready, argo-exec SA present, artifactRepository→MinIO"
