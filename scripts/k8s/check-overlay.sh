#!/usr/bin/env bash
# =============================================================================
# post-apply 自检：校验 live 资源的关键 overlay 字段未被 revert。
# 手动 `kubectl apply -f <base单文件>` 会绕过 kustomize revert overlay（ARGO_MODE/envFrom/
# __HOST_IP__），导致 pod 静默 crash。本脚本把「静默 crash」变成「立刻报错」。
# 仅 kind（云环境 dev/staging/prod 期望值不同，不在覆盖范围）。
# 退出码：0 OK / 1 有字段被 revert / 2 env 不支持
# =============================================================================
set -euo pipefail

ENV="${1:?usage: check-overlay.sh <kind>}"
[ "$ENV" = "kind" ] || { echo "仅支持 kind（云环境期望值不同）" >&2; exit 2; }

NS=apihub-system
fail=0

# (a) workflow ConfigMap：ARGO_MODE 必须 k8s（base 默认 stub）
MODE=$(kubectl -n "$NS" get cm workflow-config -o jsonpath='{.data.ARGO_MODE}' 2>/dev/null || echo "")
if [ "$MODE" != "k8s" ]; then
  echo "❌ workflow-config ARGO_MODE='$MODE'（期望 k8s）—— 被 base revert？用 scripts/k8s/apply.sh kind"
  fail=1
fi

# (b) shared-infra ConfigMap：不得残留 __HOST_IP__（host IP 注入漏了）
if kubectl -n "$NS" get cm apihub-shared-infra -o yaml 2>/dev/null | grep -q '__HOST_IP__'; then
  echo "❌ apihub-shared-infra 残留 __HOST_IP__ —— host IP 注入未生效；用 scripts/k8s/apply.sh kind"
  fail=1
fi

# (c) 每个业务 Deployment 的 envFrom 必须含 apihub-shared-infra（base 无 envFrom）
for d in api-registry dispatcher auth executor quota tenant admin docs trace retry workflow; do
  EF=$(kubectl -n "$NS" get deploy "$d" -o jsonpath='{.spec.template.spec.containers[0].envFrom}' 2>/dev/null || echo "")
  if ! echo "$EF" | grep -q apihub-shared-infra; then
    echo "❌ deploy/$d 缺 envFrom apihub-shared-infra —— 被 base revert？用 scripts/k8s/apply.sh kind"
    fail=1
  fi
done

if [ "$fail" -eq 0 ]; then
  echo "✅ overlay 自检通过（ARGO_MODE / host IP / envFrom 均未被 revert）"
  exit 0
fi
exit 1
