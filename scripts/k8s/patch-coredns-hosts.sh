#!/usr/bin/env bash
# =============================================================================
# 让 kind pod 能解析 host.docker.internal → docker-bridge IP。
#
# 背景：deploy/k8s/overlays/kind/shared-infra.yaml 的 host 字段用 host.docker.internal
# （git 可表达的稳定名称，兼容 ArgoCD GitOps）。但 kind-on-Linux 的 pod 不继承 node 的
# /etc/hosts，host.docker.internal 默认不可解析（socket.gaierror）→ 在 kube-system 的
# CoreDNS Corefile 里注入一个 hosts 块做集群范围解析。
#
# 为何不走 overlay：CoreDNS 在 kube-system，不在 overlays/kind 目录下 → 不被 ArgoCD
# Application（path=overlays/kind）管理 → selfHeal 不会回滚此运行时注入。这正是
# 「git 真相（host.docker.internal）」与「运行时注入（→ 真实 bridge IP）」分离的设计点。
#
# 幂等：已存在且 IP 一致 → 跳过；IP 过期 → 原地刷新；缺失 → 插入。可安全重复运行。
# =============================================================================
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"

KCTX="${KIND_CONTEXT:-kind-apihub}"
COREDNS_NS=kube-system

log() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
say() { printf '  %s\n' "$*"; }

# ---------- 1) docker bridge gateway（kind pod 经此访问 host compose 服务）----------
HOST_IP=$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null) \
  || { echo "FATAL: 无法读取 docker bridge gateway（docker daemon 未起？）" >&2; exit 1; }
[ -n "$HOST_IP" ] || { echo "FATAL: bridge gateway IP 为空" >&2; exit 1; }
say "docker bridge gateway = $HOST_IP"

# ---------- 2) 读当前 Corefile ----------
if ! kubectl --context "$KCTX" -n "$COREDNS_NS" get cm coredns >/dev/null 2>&1; then
  echo "FATAL: kube-system/coredns ConfigMap 不存在（context=$KCTX）" >&2; exit 1
fi
COREFILE=$(kubectl --context "$KCTX" -n "$COREDNS_NS" get cm coredns -o jsonpath='{.data.Corefile}')
printf '%s' "$COREFILE" > /tmp/coredns-before.txt

# ---------- 3) 幂等编辑：保证 hosts 块存在且 IP = $HOST_IP ----------
#    先剥掉任何含 host.docker.internal 的旧 hosts 块（含过期 IP），再统一插入新块；
#    若最终 Corefile 与修改前逐字节相同 → noop（跳过 patch + rollout，可任意重跑）。
ACTION=$(HOST_IP="$HOST_IP" python3 <<'PY'
import os, re, sys
ip = os.environ["HOST_IP"]
corefile = open("/tmp/coredns-before.txt").read()
# 剥掉既有的（含 host.docker.internal 的）hosts { ... } 块（单层大括号，无嵌套）
corefile = re.sub(r'[ \t]*hosts\s*\{[^}]*host\.docker\.internal[^}]*\}\n?', '', corefile)
# 插入新块到 .:53 { 之后
block = (
    "    hosts {\n"
    f"        {ip} host.docker.internal\n"
    "        fallthrough\n"
    "    }\n"
)
if re.search(r'\.:53\s*\{', corefile):
    corefile = re.sub(r'(\.:53\s*\{\n)', r'\1' + block, corefile, count=1)
else:
    print("FATAL: Corefile 未找到 .:53 server 块", file=sys.stderr); sys.exit(2)
open("/tmp/coredns-after.txt", "w").write(corefile)
# 唯一判据：新旧 Corefile 是否逐字节一致
print("noop" if open("/tmp/coredns-after.txt").read() == open("/tmp/coredns-before.txt").read() else "changed")
PY
)

if [ "$ACTION" = "noop" ]; then
  log "CoreDNS hosts 已是最新（$HOST_IP），无需变更 —— 跳过 patch/rollout"
  exit 0
fi

# patch cm（用 json 把整份 Corefile 原样写回，避免 strategic merge 对 data 键的拼接）
NEW_COREFILE=$(cat /tmp/coredns-after.txt)
kubectl --context "$KCTX" -n "$COREDNS_NS" patch cm coredns --type merge \
  -p "$(HOST_IP="$HOST_IP" python3 -c 'import json,os; print(json.dumps({"data":{"Corefile":open("/tmp/coredns-after.txt").read()}}))')"
say "CoreDNS Corefile 已更新（$ACTION: host.docker.internal → $HOST_IP）"

# ---------- 4) rollout restart + 等 ready（让新 Corefile 生效）----------
log "rollout restart coredns"
kubectl --context "$KCTX" -n "$COREDNS_NS" rollout restart deploy/coredns
kubectl --context "$KCTX" -n "$COREDNS_NS" rollout status deploy/coredns --timeout=120s

log "CoreDNS hosts patch OK —— host.docker.internal → $HOST_IP （集群范围生效）"
