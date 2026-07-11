#!/usr/bin/env bash
# =============================================================================
# 在 kind 装 ArgoCD（GitOps 控制面）。
#   1) fetch 官方 install.yaml（绕 host 代理坑：直连失败回退 HTTPS_PROXY）
#   2) 镜像 host pull（socks5 docker 不支持）→ crane via HTTP 代理预载 → kind load
#   3) kubectl apply + 等 ArgoCD 组件 ready
#   4) 自检
# 前提：kind 集群 kind-apihub 在；/tmp/crane 在（或脚本内兜底）。
# =============================================================================
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"

CLUSTER_NAME="${KIND_CLUSTER_NAME:-apihub}"
ARGOCD_NS=argocd
ARGOCD_VERSION="${ARGOCD_VERSION:-v2.13.2}"

log() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
say() { printf '  %s\n' "$*"; }

# ---------- 1) fetch install.yaml（绕代理）----------
log "fetch argo-cd install.yaml ($ARGOCD_VERSION) from raw manifests"
INSTALL=/tmp/argocd-install.yaml
# ArgoCD 不像 Argo Workflows 把 install.yaml 作为 release asset 发布（release 只含 CLI/SBOM），
# 官方集群安装 manifest 在仓库 manifests/install.yaml（raw.githubusercontent）。
URL="https://raw.githubusercontent.com/argoproj/argo-cd/${ARGOCD_VERSION}/manifests/install.yaml"
# github raw CDN 直连 flaky（TLS handshake timeout）；先直连，失败回退环境代理。
if ! env -u HTTPS_PROXY -u HTTP_PROXY -u https_proxy -u http_proxy -u ALL_PROXY -u all_proxy \
     curl --noproxy '*' -fsSL --retry 3 --retry-delay 2 --max-time 60 "$URL" -o "$INSTALL"; then
  say "direct fetch failed → retry via environment proxy (HTTPS_PROXY)"
  curl -fsSL --retry 3 --retry-delay 2 --max-time 120 "$URL" -o "$INSTALL"
fi
# 若版本号写错 → raw 返回 "404: Not Found"（14B 文本），给出明确提示。
# 注意：不能用 grep 'Not Found'——manifest 内 ConfigMap 注释合法含 "not found"（大小写
# 不敏感匹配会误杀）。真实 install.yaml >1MB，任何 404 响应 <10KB → 用大小阈值区分。
if [ ! -s "$INSTALL" ] || [ "$(wc -c < "$INSTALL")" -lt 10000 ]; then
  echo "FATAL: install.yaml 拉取失败（检查 ARGOCD_VERSION=$ARGOCD_VERSION 是否存在；URL=$URL）" >&2
  exit 1
fi
say "fetched $(grep -c '^kind:' "$INSTALL") manifests"

# ---------- 2) 镜像预拉 → kind load ----------
log "preload argocd images (host pull via crane → kind load)"
# install.yaml 用 image: 字段（无引号 / 双引号两种都兼容，与 argo-setup.sh 同正则）
mapfile -t IMAGES < <(grep -oE 'image:[[:space:]]*("[^"]+"|[^[:space:]]+)' "$INSTALL" \
  | sed -E 's/image:[[:space:]]*//; s/^"//; s/"$//' | sort -u)
if [ "${#IMAGES[@]}" -eq 0 ]; then
  echo "FATAL: 未从 install.yaml 抓到镜像" >&2; exit 1
fi
if ! command -v /tmp/crane >/dev/null 2>&1 && ! [ -x /tmp/crane ]; then
  echo "FATAL: /tmp/crane 不在（host docker daemon socks5 代理不支持 pull，须 crane 预载）。先按 host-proxy memory 备好 crane。" >&2
  exit 1
fi
for img in "${IMAGES[@]}"; do
  if docker image inspect "$img" >/dev/null 2>&1; then
    say "present: $img"
  else
    # 经 HTTP 代理 crane pull 偶发 stream EOF（unexpected EOF）→ 单镜像重试 2 次再放弃；
    # daemon 已载的镜像上面分支会跳过，故整脚本重跑也幂等。
    pulled=0
    for attempt in 1 2; do
      say "crane pull+load: $img (attempt $attempt)"
      if HTTPS_PROXY=http://127.0.0.1:12348 /tmp/crane pull "$img" /tmp/argocd-img.tar \
        && docker load -i /tmp/argocd-img.tar; then
        pulled=1; break
      fi
      rm -f /tmp/argocd-img.tar; say "attempt $attempt failed → retry"
    done
    [ "$pulled" -eq 1 ] || { echo "FATAL: crane pull 失败 $img（整脚本重跑可恢复，已载的会跳过）" >&2; exit 1; }
  fi
  kind load docker-image "$img" --name "$CLUSTER_NAME"
done

# ---------- 3) apply + 等 ready ----------
kubectl create namespace "$ARGOCD_NS" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
# ArgoCD install.yaml 上游把各容器 imagePullPolicy 写死 Always —— kind 离线节点上 Always 会强制
# 走 registry 校验（节点继承了 host 的 127.0.0.1:12348 代理，但该代理只在 host 上，节点内 dial
# refused）→ ImagePullBackOff（即使镜像已 kind load 到节点）。改 IfNotPresent 让预载镜像直接命中。
# （Argo Workflows manifest 默认就是 IfNotPresent，故 argo-setup.sh 无需此步。）
sed -i 's/imagePullPolicy: Always/imagePullPolicy: IfNotPresent/g' "$INSTALL"
log "kubectl apply argocd install.yaml (imagePullPolicy: Always → IfNotPresent)"
kubectl apply -n "$ARGOCD_NS" -f "$INSTALL"

# argocd-cm: 设 kustomize.buildOptions=--load-restrictor LoadRestrictionsNone。
# 所有 overlay（kind/dev/staging/prod）都引用 ../../base、../../services（在自身目录之外），
# kustomize v5 默认 LoadRestrictionsRootOnly 会拒载父目录 → ArgoCD sync 会卡在
# "Error: ... security; file is not in or below '.../overlays/kind'"。
# 本仓库 scripts/k8s/apply.sh 对所有 env 都强制该 flag；此处让 ArgoCD repo-server 的 kustomize
# 也带上（无 per-Application 的 load-restrictor override，只能全局设 buildOptions）。
# 幂等：patch 是 merge，重复设同值无副作用；rollout restart 让 repo-server 重新加载。
log "patch argocd-cm: kustomize.buildOptions (LoadRestrictionsNone) + restart server/repo-server"
kubectl -n "$ARGOCD_NS" patch cm argocd-cm --type merge \
  -p '{"data":{"kustomize.buildOptions":"--load-restrictor LoadRestrictionsNone"}}'
kubectl -n "$ARGOCD_NS" rollout restart deploy/argocd-server deploy/argocd-repo-server

log "wait argocd-server / application-controller Available"
kubectl -n "$ARGOCD_NS" wait deploy/argocd-server --for=condition=Available --timeout=300s
# application-controller 是 StatefulSet，且部分 k8s 版本下 sts 的 status.conditions 不填 Available
# （空数组）→ wait --for=condition=Available 会永久挂起。rollout status 走 readyReplicas 判定，
# 与 argo-setup.sh 的 deploy/workflow-controller 同套路。
kubectl -n "$ARGOCD_NS" rollout status sts/argocd-application-controller --timeout=300s

# ---------- 4) 自检 ----------
log "self-check"
kubectl get crd applications.argoproj.io >/dev/null
kubectl -n "$ARGOCD_NS" get deploy argocd-server >/dev/null
kubectl -n "$ARGOCD_NS" get deploy argocd-repo-server >/dev/null
kubectl -n "$ARGOCD_NS" get sts argocd-application-controller >/dev/null
say "ARGOCD SETUP OK —— server/repo-server/application-controller Available + CRD 注册"
