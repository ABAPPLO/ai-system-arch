#!/usr/bin/env bash
# =============================================================================
# 唯一 K8s apply 入口。
# 禁止直接 `kubectl apply -f deploy/k8s/...`（绕过 kustomize 会 revert overlay patch，
# 详见 docs/phase2-integration-findings.md「kind overlay 脚踩坑」）。
# 用法：scripts/k8s/apply.sh <env>   env ∈ {kind, dev, staging, prod}
#   kind           本地 kind：shared-infra host 用 host.docker.internal（git 真相，selfHeal 不回滚）；
#                  apply 后①patch-coredns-hosts.sh 注入 host→bridge IP 解析（CoreDNS 运行时层），
#                  ②patch_shared_infra_ports read-back compose 实际端口并 patch 进 CM（端口运行时层；
#                  CM 标 Compare=false → ArgoCD 不回滚）。
#   dev/staging/prod 远端 ACK：纯 kustomize build | apply（云上走 in-cluster DNS）
# =============================================================================
set -euo pipefail

# ---------- helpers ----------
# read-back compose 实际 publish 的 host 端口：host 5432/6379/9000 被占时 compose 经
# pick_host_port（bootstrap.sh）重映射到高位端口，与 git-truth 标准端口不一致。
# 用法：rb <container> <container_port>  → echo host_port（空若未 publish）。
rb() { docker port "$1" "$2" 2>/dev/null | awk -F: '/^0\.0\.0\.0:/{print $NF; exit}'; }

# kind 专属：read-back compose 实际 publish 端口并 patch 进 shared-infra CM。
# 前提：CM 已被 kustomize apply 创建（在调用前），shared-infra 标 Compare=false → ArgoCD 不回滚。
# 幂等：重复运行只重新 patch 同值（kubectl patch 本身幂等）。
patch_shared_infra_ports() {
  # 各端口：read-back compose；空则回退标准值（容器未起 / 未 publish 时仍写一个合理默认）。
  local pg_hp redis_hp ch_hp kafka_hp otel_hp
  pg_hp=$(rb apihub-pg 5432);        pg_hp=${pg_hp:-5432}
  redis_hp=$(rb apihub-redis 6379);  redis_hp=${redis_hp:-6379}
  ch_hp=$(rb apihub-clickhouse 8123); ch_hp=${ch_hp:-8123}
  kafka_hp=$(rb apihub-kafka 9094);  kafka_hp=${kafka_hp:-9094}
  otel_hp=$(rb apihub-otel 4317);    otel_hp=${otel_hp:-4317}
  echo "== patch shared-infra CM ports (Compare=false → ArgoCD skips): pg=$pg_hp redis=$redis_hp ch=$ch_hp kafka=$kafka_hp otel=$otel_hp =="
  # 端口字段（PG/REDIS/CH）直写；KAFKA/OTEL 是 host:port 整串 → 整串替换。
  kubectl -n apihub-system patch cm apihub-shared-infra --type merge -p "$(
    PG_HP="$pg_hp" REDIS_HP="$redis_hp" CH_HP="$ch_hp" KAFKA_HP="$kafka_hp" OTEL_HP="$otel_hp" \
    python3 -c '
import json, os
k = os.environ["KAFKA_HP"]; o = os.environ["OTEL_HP"]
print(json.dumps({"data": {
    "PG_PORT":   os.environ["PG_HP"],
    "REDIS_PORT": os.environ["REDIS_HP"],
    "CH_PORT":   os.environ["CH_HP"],
    "KAFKA_BROKERS": "host.docker.internal:" + k,
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://host.docker.internal:" + o,
}}))'
  )"
}

ENV="${1:?usage: apply.sh <kind|dev|staging|prod>}"
case "$ENV" in
  kind)
    OVERLAY=deploy/k8s/overlays/kind
    # shared-infra.yaml 的 host 用 host.docker.internal（git 真相，ArgoCD selfHeal 不回滚）；
    # 端口为运行时值（host 5432/6379 被占 → compose pick_host_port 重映射到高位端口），
    # shared-infra 标 Compare=false 让 ArgoCD 跳过该 CM，apply 后由下方 port-patch 把 compose
    # 实际 publish 端口 read-back 并 patch 进 live CM（闭环不回滚）。host.docker.internal →
    # bridge IP 的解析由 patch-coredns-hosts.sh 注入（CoreDNS 在 kube-system，不受 GitOps 管）。
    LOAD_REST="--load-restrictor LoadRestrictionsNone"
    say_host=""
    ;;
  dev|staging|prod)
    OVERLAY=deploy/k8s/overlays/$ENV
    # dev/staging/prod overlay 同样引用 ../../base ../../services（与 kind 同构），
    # kustomize v5 默认 LoadRestrictionsRootOnly 会拒载父目录 → 必须 LoadRestrictionsNone。
    LOAD_REST="--load-restrictor LoadRestrictionsNone"
    say_host=""
    ;;
  *)
    echo "unknown env: $ENV (expect kind|dev|staging|prod)" >&2
    exit 2
    ;;
esac

echo "== kustomize build + apply ($ENV) $say_host =="
kustomize build $LOAD_REST "$OVERLAY" | kubectl apply -f -
echo "== apply ($ENV) done. =="
# kind：注入 host.docker.internal → docker-bridge IP 的集群范围 DNS 解析（CoreDNS，运行时层）。
#       随后 read-back compose 实际 publish 的 host 端口并 patch 进 shared-infra CM（端口运行时层）：
#       host 5432/6379/9000 被占时 compose 经 pick_host_port 重映射，git-truth 的标准端口与运行时
#       不一致；shared-infra 标 Compare=false → ArgoCD 不回滚本 patch。两步都在 apply 之后（CM 已存在）。
if [ "$ENV" = kind ]; then
  bash "$(dirname "$0")/patch-coredns-hosts.sh"
  patch_shared_infra_ports
fi
# check-overlay.sh 仅支持 kind，ACK env（dev/staging/prod）跟跑会 exit 2，故仅 kind 提示。
[ "$ENV" = kind ] && echo "建议跟跑：scripts/k8s/check-overlay.sh $ENV"
