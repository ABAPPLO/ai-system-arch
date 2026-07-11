#!/usr/bin/env bash
# =============================================================================
# 唯一 K8s apply 入口。
# 禁止直接 `kubectl apply -f deploy/k8s/...`（绕过 kustomize 会 revert overlay patch，
# 详见 docs/phase2-integration-findings.md「kind overlay 脚踩坑」）。
# 用法：scripts/k8s/apply.sh <env>   env ∈ {kind, dev, staging, prod}
#   kind           本地 kind：shared-infra.yaml 已移出 kustomization（移出 ArgoCD 管理）。
#                  host 用 host.docker.internal（git 真相）；apply 时①kustomize build|apply 下发
#                  ArgoCD 管控资源，②standalone apply shared-infra.yaml（不打 instance 标签 →
#                  ArgoCD 跟踪不到），③patch-coredns-hosts.sh 注入 host→bridge IP 解析（CoreDNS 运行时层），
#                  ④patch_shared_infra_ports read-back compose 实际端口 patch 进 CM（端口运行时层）。
#                  CM 脱离 ArgoCD → patch 永不被 selfHeal 回滚。
#   dev/staging/prod 远端 ACK：纯 kustomize build | apply（云上走 in-cluster DNS）
# =============================================================================
set -euo pipefail

# ---------- helpers ----------
# read-back compose 实际 publish 的 host 端口：host 5432/6379/9000 被占时 compose 经
# pick_host_port（bootstrap.sh）重映射到高位端口，与 git-truth 标准端口不一致。
# 用法：rb <container> <container_port>  → echo host_port（空若未 publish）。
rb() { docker port "$1" "$2" 2>/dev/null | awk -F: '/^0\.0\.0\.0:/{print $NF; exit}'; }

# kind 专属：read-back compose 实际 publish 端口并 patch 进 shared-infra CM。
# 前提：CM 已被本脚本上游 standalone apply（kubectl apply -f shared-infra.yaml）创建。
# shared-infra 已移出 ArgoCD 管理（无 instance 标签）→ patch 永不被 selfHeal 回滚。
# 幂等：重复运行只重新 patch 同值（kubectl patch 本身幂等）。
patch_shared_infra_ports() {
  # 各端口：read-back compose；空则回退标准值（容器未起 / 未 publish 时仍写一个合理默认）。
  local pg_hp redis_hp ch_hp kafka_hp otel_hp
  pg_hp=$(rb apihub-pg 5432);        pg_hp=${pg_hp:-5432}
  redis_hp=$(rb apihub-redis 6379);  redis_hp=${redis_hp:-6379}
  ch_hp=$(rb apihub-clickhouse 8123); ch_hp=${ch_hp:-8123}
  kafka_hp=$(rb apihub-kafka 9094);  kafka_hp=${kafka_hp:-9094}
  otel_hp=$(rb apihub-otel 4317);    otel_hp=${otel_hp:-4317}
  echo "== patch shared-infra CM ports (CM out of ArgoCD scope → no selfHeal revert): pg=$pg_hp redis=$redis_hp ch=$ch_hp kafka=$kafka_hp otel=$otel_hp =="
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
    # shared-infra.yaml 已从 kustomization resources 移除（移出 ArgoCD 管理）。host 用
    # host.docker.internal（git 真相）；端口为运行时值（host 5432/6379 被占 → compose
    # pick_host_port 重映射到高位端口）。本脚本在 kustomize apply 之后 standalone apply
    # shared-infra.yaml（不打 instance 标签 → ArgoCD 跟踪不到 → 不 prune/不 selfHeal），
    # 再由 patch_shared_infra_ports 把 compose 实际 publish 端口 read-back 并 patch 进
    # live CM（CM 脱离 ArgoCD → patch 永不回滚）。host.docker.internal → bridge IP 的解析
    # 由 patch-coredns-hosts.sh 注入（CoreDNS 在 kube-system，不受 GitOps 管）。
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
# kind：shared-infra.yaml 已从 kustomization resources 移除（移出 ArgoCD 管理）→ 必须在此 standalone
#       apply（否则首次部署 / CM 被删后无重建来源）。standalone kubectl apply 不会打 ArgoCD 的
#       app.kubernetes.io/instance 标签 → ArgoCD 既不在 manifest 里、也跟踪不到它 → selfHeal/prune 不碰。
#       随后注入 host.docker.internal → docker-bridge IP 解析（CoreDNS 运行时层），
#       并 read-back compose 实际 publish 的 host 端口 patch 进 shared-infra CM（端口运行时层）：
#       host 5432/6379/9000 被占时 compose 经 pick_host_port 重映射，与 git 标准端口不一致；
#       CM 已脱离 ArgoCD → patch 永不被 selfHeal 回滚。
if [ "$ENV" = kind ]; then
  echo "== standalone apply shared-infra.yaml (out of ArgoCD scope) =="
  kubectl apply -f "$OVERLAY/shared-infra.yaml"
  bash "$(dirname "$0")/patch-coredns-hosts.sh"
  patch_shared_infra_ports
fi
# check-overlay.sh 仅支持 kind，ACK env（dev/staging/prod）跟跑会 exit 2，故仅 kind 提示。
[ "$ENV" = kind ] && echo "建议跟跑：scripts/k8s/check-overlay.sh $ENV"
