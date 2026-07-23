#!/usr/bin/env bash
# scripts/k8s/sso-mock-e2e.sh
#
# Admin 钉钉 SSO 全链 e2e（mock-mode）。部署 auth 新镜像到 kind 且开启
# DINGTALK_MOCK_MODE=true + BOOTSTRAP_ADMIN_DINGTALK_UNIONIDS 后运行：
#   kubectl -n apihub-system port-forward deploy/auth 8002:8002 &
#   kubectl -n apihub-system port-forward deploy/admin 8006:8006 &
#   ./scripts/k8s/sso-mock-e2e.sh
#
# mock 协议（auth dingtalk.py）：code = mock:<unionId>:<name>
# 验证：authorize→callback→JWT(is_platform_admin) / 重放 state→401 / 非超管→false。
set -euo pipefail

AUTH_HOST="${AUTH_HOST:-http://localhost:8002}"
ADMIN_HOST="${ADMIN_HOST:-http://localhost:8006}"
ADMIN_UID="${ADMIN_UID:-UID_ADMIN}"          # 须在 BOOTSTRAP_ADMIN_DINGTALK_UNIONIDS 中
OTHER_UID="${OTHER_UID:-UID_OTHER}"
REDIRECT="${REDIRECT:-http://localhost:5173/login/callback}"

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing: $1" >&2; exit 1; }; }
need curl; need jq

echo "== 1. authorize 取 state =="
STATE=$(curl -fsS "${AUTH_HOST}/v1/auth/dingtalk/authorize?redirect=${REDIRECT}" | jq -r .state)
[ -n "$STATE" ] || { echo "FAIL: empty state"; exit 1; }
echo "   state=${STATE}"

echo "== 2. callback（mock code = mock:${ADMIN_UID}:Admin）→ JWT =="
RESP=$(curl -fsS -X POST "${AUTH_HOST}/v1/auth/dingtalk/callback" \
  -H 'Content-Type: application/json' \
  -d "{\"code\":\"mock:${ADMIN_UID}:Admin\",\"state\":\"${STATE}\"}")
JWT=$(echo "$RESP" | jq -r .access_token)
IS_ADMIN=$(echo "$RESP" | jq -r .user.is_platform_admin)
echo "   is_platform_admin=${IS_ADMIN} (expect true)"
[ "$IS_ADMIN" = "true" ] || { echo "FAIL: bootstrap admin not flagged"; exit 1; }

echo "== 3. 用 Bearer JWT 调 admin dashboard（期望 200）=="
CODE=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer ${JWT}" "${ADMIN_HOST}/v1/admin/dashboard")
echo "   admin dashboard HTTP=${CODE} (expect 200)"
[ "$CODE" = "200" ] || { echo "FAIL: admin rejected valid JWT (${CODE})"; exit 1; }

echo "== 4. 重放同一 state（已消费）→ 期望 401 =="
REPLAY=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${AUTH_HOST}/v1/auth/dingtalk/callback" \
  -H 'Content-Type: application/json' \
  -d "{\"code\":\"mock:${ADMIN_UID}:Admin\",\"state\":\"${STATE}\"}")
echo "   replay HTTP=${REPLAY} (expect 401)"
[ "$REPLAY" = "401" ] || { echo "FAIL: state replay not rejected (${REPLAY})"; exit 1; }

echo "== 5. 非超管 unionId (${OTHER_UID}) → is_platform_admin=false =="
S2=$(curl -fsS "${AUTH_HOST}/v1/auth/dingtalk/authorize?redirect=${REDIRECT}" | jq -r .state)
OTHER=$(curl -fsS -X POST "${AUTH_HOST}/v1/auth/dingtalk/callback" \
  -H 'Content-Type: application/json' \
  -d "{\"code\":\"mock:${OTHER_UID}:Other\",\"state\":\"${S2}\"}")
OTHER_IS=$(echo "$OTHER" | jq -r .user.is_platform_admin)
echo "   is_platform_admin=${OTHER_IS} (expect false)"
[ "$OTHER_IS" = "false" ] || { echo "FAIL: non-bootstrap user flagged admin"; exit 1; }

echo "ALL PASS: SSO mock-mode 全链 OK（admin JWT 鉴权 / state 一次性 / 超管判定）"
