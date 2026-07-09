#!/usr/bin/env bash
# =============================================================================
# kind bootstrap：起 kind 集群 + 复用 host compose 数据层 + 构建 11 镜像 + load
#                + apply overlay + 等 ready + 健康抽检。
#
# 相对 task brief 的修正/补强：
#   1) kustomize build 使用 --load-restrictor LoadRestrictionsNone（overlay 引用
#      ../../base、../../services，在自身目录之外）。
#   2) apply 后 git checkout 还原 shared-infra.yaml 的 __HOST_IP__ 占位符（提交文件保持干净）。
#   3) Kafka 显式健康探测（docker exec kafka-broker-api-versions.sh）。
#
# 本机环境补强（brief 假设干净 host；本机为共享机，多端口被占）：
#   4) redis(6379)/postgres(5432) 的 host 端口被 host 系统服务占用 → 自动探测空闲
#      高位端口并通过 compose override 重映射；overlay 的 REDIS_PORT/PG_PORT 同步改写，
#      使 kind pod 仍能经 HOST_IP:port 连到对应数据服务。
#   5) 只起 pod 依赖的数据服务（postgres redis kafka kafka-init clickhouse jaeger
#      otel-collector）；minio/grafana/prometheus 与 pod 无关且端口被占，跳过。
# =============================================================================
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
cd "$(dirname "$0")/../.."

# ---------- helpers ----------
# host_port_free PORT: 0 if free, 1 if any host listener or docker publish occupies it.
host_port_free() {
  local p=$1
  if ss -ltn 2>/dev/null | awk '{print $4}' | awk -F: '{print $NF}' | grep -qx "$p"; then return 1; fi
  if docker ps --format '{{.Ports}}' 2>/dev/null | grep -qE "0\.0\.0\.0:$p->|:::$p->"; then return 1; fi
  return 0
}

# pick_host_port NAME DEFAULT: 若 DEFAULT 空闲则用之；否则从 DEFAULT+10000 起向上找首个空闲端口。
pick_host_port() {
  local svc=$1 def=$2 cand
  if host_port_free "$def"; then echo "$def"; return; fi
  cand=$((def + 10000))
  while [ "$cand" -lt 65535 ]; do
    if host_port_free "$cand"; then echo "$cand"; return; fi
    cand=$((cand + 1))
  done
  echo "FATAL: no free host port for $svc (base $def)" >&2; exit 1
}

# 0) 探测 host 网桥 IP（kind pod 经此访问 host compose 服务）
HOST_IP=$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}')
echo "host bridge gateway: $HOST_IP"

# 1) 确保 compose 数据层在跑 + Kafka advertize 指向 host 网桥
grep -q '^KAFKA_EXTERNAL_HOST=' .env.dev 2>/dev/null || echo "KAFKA_EXTERNAL_HOST=$HOST_IP" >> .env.dev

# 1a) redis/pg host 端口冲突 → 自动重映射
REDIS_HP=$(pick_host_port redis 6379)
PG_HP=$(pick_host_port postgres 5432)
echo "host ports: redis=$REDIS_HP (default 6379)  postgres=$PG_HP (default 5432)"

# 1b) 生成 compose override（!override 替换而非追加 ports 列表）
#     postgres command: 11 服务 × 4 uvicorn worker × pool(min 10) 轻松 > PG 默认
#     max_connections=100 → TooManyConnections 让晚启动的服务 CrashLoop。dev/kind 下放宽到 500
#     （仅本 override 生效，不改提交的 compose）。
OVR=/tmp/kind-compose-override.yml
{
  echo "services:"
  echo "  redis:"
  echo "    ports: !override"
  echo "      - \"$REDIS_HP:6379\""
  echo "  postgres:"
  echo "    command: [\"postgres\", \"-c\", \"max_connections=500\"]"
  echo "    ports: !override"
  echo "      - \"$PG_HP:5432\""
} > "$OVR"

# 1c) 起 pod 依赖的数据服务（跳过 minio/grafana/prometheus —— 端口被占且 pod 不依赖）。
#     不用 --wait：clickhouse 的 Kafka-engine 物化视图不稳（间歇 connection refused），
#     会拖垮整体 --wait；CH 仅 trace 服务依赖，单列探测、非致命。
docker compose --env-file .env.dev -f docker-compose.dev.yml -f "$OVR" \
  up -d postgres redis kafka kafka-init clickhouse jaeger otel-collector

# wait_ready NAME CMD... ：轮询直到 CMD 成功或超时（致命）
wait_ready() { local name=$1; shift; for i in $(seq 1 40); do if "$@" >/dev/null 2>&1; then echo "$name OK"; return 0; fi; sleep 3; done; echo "FATAL: $name not ready"; exit 1; }

# 1d) Kafka broker 显式可达性（advertize host = $HOST_IP:9094）
echo "=== probing kafka on $HOST_IP:9094 ==="
wait_ready "kafka(via $HOST_IP:9094)" docker exec apihub-kafka kafka-broker-api-versions.sh --bootstrap-server "$HOST_IP:9094"
echo "=== pg / redis readiness ==="
wait_ready "postgres" docker exec apihub-pg pg_isready -U apihub_app -d apihub
wait_ready "redis" docker exec apihub-redis redis-cli -a "${REDIS_PASSWORD:-apihub_dev_pwd}" ping

# 1e) ClickHouse 仅 trace 依赖：探测但不致命（trace 的 ready 会因此 fail，单列记录）
if docker exec apihub-clickhouse wget -qO- http://localhost:8123/ping >/dev/null 2>&1; then
  echo "clickhouse OK"
else
  echo "WARN: clickhouse 8123 not reachable (Kafka-engine MV 不稳)；仅影响 trace 服务 readiness"
fi

# 2) 创建 kind 集群（预留 APISIX NodePort 30080）
cat >/tmp/kind-config.yaml <<EOF
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
    extraPortMappings:
      - containerPort: 30080
        hostPort: 30080
EOF
kind delete cluster --name apihub 2>/dev/null || true
kind create cluster --name apihub --config /tmp/kind-config.yaml
kubectl config use-context kind-apihub

# 3) 注入 host IP + 重映射端口到 overlay；apply 后还原占位符
#
#    端口同步不变式：overlay 写入的 REDIS_PORT/PG_PORT 必须 == compose 实际 publish
#    的 host 端口。Task 8 smoke 踩过 off-by-one（redis publish 在 16381 而 ConfigMap
#    写的是 16380 → pod 连不上 Redis），根因是「pick 的端口」「override publish 的端口」
#    「overlay 写入的端口」三处独立写入、可人为/笔误错位。此处统一改为以 `docker port`
#    read-back 的【实际 publish 端口】作为唯一真源写入 overlay：override 用 pick 值发布，
#    compose up 之后读回真实 host 端口，再用它写 overlay —— 从结构上保证 publish==overlay，
#    即使 pick 与实际偶发错位（端口被抢占/override 未生效/旧容器残留）也能自我纠正。
#    Kafka 固定 9094（host 未占用、且 advertize 指向 host:9094，不重映射）compose 与 overlay
#    两处均为 9094，天然一致，无需 read-back。
read_compose_host_port() {  # NAME CONTAINER_PORT -> 实际 publish 的 host 端口
  docker port "$1" "$2" 2>/dev/null | awk -F: '/^0\.0\.0\.0:/ {print $NF; exit}'
}
REDIS_HP=$(read_compose_host_port apihub-redis 6379)
PG_HP=$(read_compose_host_port apihub-pg 5432)
[ -n "$REDIS_HP" ] || { echo "FATAL: apihub-redis host port read-back empty" >&2; exit 1; }
[ -n "$PG_HP" ]    || { echo "FATAL: apihub-pg host port read-back empty" >&2; exit 1; }
echo "overlay-sync host ports (read back from compose): redis=$REDIS_HP pg=$PG_HP  (kafka fixed 9094)"
sed -i "s/__HOST_IP__/$HOST_IP/g" deploy/k8s/overlays/kind/shared-infra.yaml
sed -i "s/^\(\s*PG_PORT:\s*\"\)5432/\1$PG_HP/" deploy/k8s/overlays/kind/shared-infra.yaml
sed -i "s/^\(\s*REDIS_PORT:\s*\"\)6379/\1$REDIS_HP/" deploy/k8s/overlays/kind/shared-infra.yaml
trap 'git checkout deploy/k8s/overlays/kind/shared-infra.yaml 2>/dev/null || true' EXIT

# 4) 构建 11 镜像 + load 进 kind
SVC=(api-registry dispatcher auth executor quota tenant admin docs trace retry workflow)
for s in "${SVC[@]}"; do
  echo "=== build+load $s ==="
  docker build -f "services/services/$s/Dockerfile" \
    -t "registry.apihub.internal/apihub/$s:0.1.0-dev" .
  kind load docker-image "registry.apihub.internal/apihub/$s:0.1.0-dev" --name apihub
done

# 4b) mock-backend 用 python:3.11-slim（非 11 服务镜像），单独 load 进 kind，否则 ErrImagePull
docker image inspect python:3.11-slim >/dev/null 2>&1 || docker pull python:3.11-slim
kind load docker-image python:3.11-slim --name apihub

# 5) apply（--load-restrictor：overlay 引用上级目录资源）
kustomize build --load-restrictor LoadRestrictionsNone deploy/k8s/overlays/kind | kubectl apply -f -

# 6) 等 ready（PSA restricted 下可能因 seccompProfile/securityContext 缺失而被拒，
#    届时 kubectl wait 会超时——参见日志中 ReplicaSet FailedCreate 事件）
kubectl wait --for=condition=ready pods -n apihub-system --all --timeout=300s

# 7) 健康抽检
kubectl -n apihub-system port-forward svc/api-registry 18000:80 &
PFS=$!
sleep 3
curl -sf http://127.0.0.1:18000/health/ready && echo " <- api-registry ready"
kill $PFS 2>/dev/null || true
echo "DONE: kind stack up. host_ip=$HOST_IP redis=$REDIS_HP pg=$PG_HP"
