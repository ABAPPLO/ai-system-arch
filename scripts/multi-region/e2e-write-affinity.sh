#!/bin/bash
# 真入口驱动（审计 §6）：注册一个 home_region=bj 的 consumer，POST 经 APISIX(sh)，
# 断言 302 + Location 指向 GATEWAY_URL_BJ。需 kind 里 APISIX 已加载 tenant-affinity 插件。
#
# 端口/密钥默认值对齐本仓库 scripts/kind/apisix-setup.sh 实际部署：
#   - APISIX_PROXY: kind NodePort 30080（gateway.type=NodePort，http.nodePort=30080）
#   - APISIX_ADMIN: admin ClusterIP 9180（需先 kubectl port-forward svc/apisix-admin 9180:9180）
#   - APISIX_ADMIN_KEY: chart v2.16.0 不接受自定义 admin_key，实测生效值 = APISIX 默认 edd1c9f0...
#     （已由 scripts/kind/apisix-setup.sh §6 从 ConfigMap config.yaml 的 admin_key 段确证）
set -euo pipefail
APISIX_ADMIN="${APISIX_ADMIN:-http://localhost:9180/apisix/admin}"
APISIX_PROXY="${APISIX_PROXY:-http://localhost:30080}"
ADMIN_KEY="${APISIX_ADMIN_KEY:-edd1c9f034335f136f87ad84b625c8f1}"

# 1. upsert consumer with home_region=bj
curl -sf -X PUT "$APISIX_ADMIN/consumers/c_bj" \
  -H "X-API-KEY: $ADMIN_KEY" -H 'Content-Type: application/json' \
  -d '{"username":"c_bj","plugins":{"key-auth":{"key":"sekret","header":"X-API-Key"}},"labels":{"home_region":"bj"}}'

# 2. 一条临时 route 启用 tenant-affinity（POST /probe/*）
curl -sf -X PUT "$APISIX_ADMIN/routes/r_probe" \
  -H "X-API-KEY: $ADMIN_KEY" -H 'Content-Type: application/json' \
  -d '{"uri":"/probe/*","methods":["POST","GET"],"upstream":{"type":"roundrobin","nodes":{"webhook.invalid:80":1}},"plugins":{"key-auth":{"header":"X-API-Key"},"tenant-affinity":{}}}'

# 3. POST 非 home → 期望 302
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$APISIX_PROXY/probe/x" -H "X-API-Key: sekret")
LOC=$(curl -s -D - -o /dev/null -X POST "$APISIX_PROXY/probe/x" -H "X-API-Key: sekret" | tr -d '\r' | awk -F': ' '/^[Ll]ocation/{print $2}')
echo "POST status=$CODE Location=$LOC"
[ "$CODE" = "302" ] || { echo "FAIL: expected 302 got $CODE"; exit 1; }
case "$LOC" in *api-bj.apihub.com*) echo "OK: 302 → bj gateway" ;; *) echo "FAIL: Location=$LOC"; exit 1 ;; esac

# 4. GET（读）应放行（非 302）
CODE_GET=$(curl -s -o /dev/null -w "%{http_code}" "$APISIX_PROXY/probe/x" -H "X-API-Key: sekret")
echo "GET status=$CODE_GET (expect != 302)"
[ "$CODE_GET" != "302" ] || { echo "FAIL: GET should not 302"; exit 1; }
echo "e2e-write-affinity PASS"
