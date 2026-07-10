#!/usr/bin/env bash
# =============================================================================
# 唯一 K8s apply 入口。
# 禁止直接 `kubectl apply -f deploy/k8s/...`（绕过 kustomize 会 revert overlay patch，
# 详见 docs/phase2-integration-findings.md「kind overlay 脚踩坑」）。
# 用法：scripts/k8s/apply.sh <env>   env ∈ {kind, dev, staging, prod}
#   kind           本地 kind：注入 host IP + read-back 端口到 shared-infra.yaml，apply 后还原
#   dev/staging/prod 远端 ACK：纯 kustomize build | apply（云上走 in-cluster DNS）
# =============================================================================
set -euo pipefail

ENV="${1:?usage: apply.sh <kind|dev|staging|prod>}"
case "$ENV" in
  kind)
    OVERLAY=deploy/k8s/overlays/kind
    HOST_IP=$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}')
    SHARED=deploy/k8s/overlays/kind/shared-infra.yaml
    # host IP + read-back 实际 publish 端口（抽自 bootstrap.sh:126-138，保证 publish==overlay）
    PG_HP=$(docker port apihub-pg 5432 2>/dev/null | awk -F: '/^0\.0\.0\.0:/{print $NF;exit}')
    REDIS_HP=$(docker port apihub-redis 6379 2>/dev/null | awk -F: '/^0\.0\.0\.0:/{print $NF;exit}')
    [ -n "$PG_HP" ]    || { echo "FATAL: apihub-pg host port read-back empty" >&2; exit 1; }
    [ -n "$REDIS_HP" ] || { echo "FATAL: apihub-redis host port read-back empty" >&2; exit 1; }
    sed -i "s/__HOST_IP__/$HOST_IP/g" "$SHARED"
    sed -i "s/^\(\s*PG_PORT:\s*\"\)5432/\1$PG_HP/" "$SHARED"
    sed -i "s/^\(\s*REDIS_PORT:\s*\"\)6379/\1$REDIS_HP/" "$SHARED"
    trap 'git checkout "$SHARED" 2>/dev/null || true' EXIT
    LOAD_REST="--load-restrictor LoadRestrictionsNone"
    say_host="(host_ip=$HOST_IP pg=$PG_HP redis=$REDIS_HP)"
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
echo "== apply ($ENV) done. 建议跟跑：scripts/k8s/check-overlay.sh $ENV =="
