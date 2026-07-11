#!/usr/bin/env bash
# =============================================================================
# 唯一 K8s apply 入口。
# 禁止直接 `kubectl apply -f deploy/k8s/...`（绕过 kustomize 会 revert overlay patch，
# 详见 docs/phase2-integration-findings.md「kind overlay 脚踩坑」）。
# 用法：scripts/k8s/apply.sh <env>   env ∈ {kind, dev, staging, prod}
#   kind           本地 kind：shared-infra 已用 host.docker.internal（git 真相，ArgoCD selfHeal
#                  不再回滚）；apply 后调 patch-coredns-hosts.sh 注入集群范围 DNS 解析（运行时层，
#                  CoreDNS 不在 overlay 目录 → 不受 GitOps 管）。
#   dev/staging/prod 远端 ACK：纯 kustomize build | apply（云上走 in-cluster DNS）
# =============================================================================
set -euo pipefail

ENV="${1:?usage: apply.sh <kind|dev|staging|prod>}"
case "$ENV" in
  kind)
    OVERLAY=deploy/k8s/overlays/kind
    # shared-infra.yaml 已用 host.docker.internal（git 真相）—— 不再 sed 注入 __HOST_IP__，
    # 也不再 read-back 端口（compose 端口固定：PG 5432 / Redis 6379 / Kafka 9094 / CH 8123）。
    # host.docker.internal → docker-bridge IP 的解析由 apply 后的 patch-coredns-hosts.sh 注入
    # （CoreDNS 在 kube-system，不在 overlay 目录，ArgoCD selfHeal 管不到 → 闭环不回滚）。
    # 注意：若 host 的 5432/6379 被占而 compose 重映射（见 bootstrap.sh pick_host_port），
    # git-truth 端口与运行时端口会不一致 —— 需单独的运行时端口注入方案（见 task-2-fix-report）。
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
if [ "$ENV" = kind ]; then
  bash "$(dirname "$0")/patch-coredns-hosts.sh"
fi
# check-overlay.sh 仅支持 kind，ACK env（dev/staging/prod）跟跑会 exit 2，故仅 kind 提示。
[ "$ENV" = kind ] && echo "建议跟跑：scripts/k8s/check-overlay.sh $ENV"
