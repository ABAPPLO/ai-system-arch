#!/usr/bin/env bash
# =============================================================================
# kind bootstrap：起 kind 集群 + 复用 host compose 数据层 + 构建 11 镜像 + load
#                + apply overlay + 等 ready + 健康抽检。
#
# 相对 task brief 的修正/补强：
#   1) kustomize build 使用 --load-restrictor LoadRestrictionsNone（overlay 引用
#      ../../base、../../services，在自身目录之外）。
#   2) shared-infra.yaml 用 host.docker.internal（git 真相，无需 sed 注入 / git checkout 还原）；
#      apply 后调 patch-coredns-hosts.sh 注入集群范围 DNS 解析（host.docker.internal → bridge IP）。
#   3) Kafka 显式健康探测（docker exec kafka-broker-api-versions.sh）。
#
# 本机环境补强（brief 假设干净 host；本机为共享机，多端口被占）：
#   4) redis(6379)/postgres(5432) 的 host 端口被 host 系统服务占用 → 自动探测空闲
#      高位端口并通过 compose override 重映射；overlay 的 REDIS_PORT/PG_PORT 同步改写，
#      使 kind pod 仍能经 HOST_IP:port 连到对应数据服务。
#   5) 起 pod 依赖的数据服务（postgres redis kafka kafka-init clickhouse jaeger
#      otel-collector minio minio-init）；minio 经 pick_host_port 重映射到空闲端口，
#      供 Argo artifactRepository 使用；grafana/prometheus 与 pod 无关且端口被占，跳过。
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
MINIO_HP=$(pick_host_port minio 9000)
echo "host ports: redis=$REDIS_HP (default 6379)  postgres=$PG_HP (default 5432)  minio=$MINIO_HP (default 9000)"

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
  echo "  minio:"
  echo "    ports: !override"
  echo "      - \"$MINIO_HP:9000\""
} > "$OVR"

# 1c) 起 pod 依赖的数据服务（minio 经 override 重映射，供 Argo artifactRepository；
#     grafana/prometheus 端口被占且 pod 不依赖，跳过）。
#     不用 --wait：clickhouse 的 Kafka-engine 物化视图不稳（间歇 connection refused），
#     会拖垮整体 --wait；CH 仅 trace 服务依赖，单列探测、非致命。

# 1c-pre) 清残留 apihub-* 容器与网络（含跨 compose project 的孤儿，如旧 apihub-pg）。
#   否则 `up` 时名字冲突 → 容器不重建 → 旧配置残留（Kafka advertised 指向错地址、
#   redis/pg 端口漂移），pod 连不上、CrashLoop。bootstrap 是重建性脚本，清空合理。
docker rm -f $(docker ps -aq --filter "name=apihub-") >/dev/null 2>&1 || true
docker network rm apihub-dev >/dev/null 2>&1 || true

docker compose --env-file .env.dev -f docker-compose.dev.yml -f "$OVR" \
  up -d postgres redis kafka kafka-init clickhouse jaeger otel-collector minio minio-init

# wait_ready NAME CMD... ：轮询直到 CMD 成功或超时（致命）
wait_ready() { local name=$1; shift; for i in $(seq 1 40); do if "$@" >/dev/null 2>&1; then echo "$name OK"; return 0; fi; sleep 3; done; echo "FATAL: $name not ready"; exit 1; }

# 1d) Kafka broker 显式可达性（advertize host = $HOST_IP:9094）
echo "=== probing kafka on $HOST_IP:9094 ==="
wait_ready "kafka(via $HOST_IP:9094)" docker exec apihub-kafka kafka-broker-api-versions.sh --bootstrap-server "$HOST_IP:9094"
echo "=== pg / redis readiness ==="
wait_ready "postgres" docker exec apihub-pg pg_isready -U apihub_app -d apihub
wait_ready "redis" docker exec apihub-redis redis-cli -a "${REDIS_PASSWORD:-apihub_dev_pwd}" ping

# 1f) 幂等回放 init-db（pg-data 卷可能旧，首启未含后加脚本；脚本全幂等可安全回放）
echo "=== apply-db (idempotent init-db replay) ==="
bash scripts/k8s/apply-db.sh

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

# 3) compose 实际 publish 端口 read-back（仅诊断 echo，不再写 overlay）
#
#    shared-infra.yaml 已用 host.docker.internal（git 真相，不再 sed 注入 __HOST_IP__）；
#    compose 端口固定（PG 5432 / Redis 6379 / Kafka 9094 / CH 8123 / OTel 4317），overlay
#    端口 == compose 声明端口，无需同步写入。host.docker.internal → bridge IP 的解析由
#    第 5 步 apply 后调 patch-coredns-hosts.sh 注入 CoreDNS（运行时层，不受 GitOps 管）。
#
#    此处保留 read-back 仅为 echo 诊断（看 compose 是否因端口冲突走了 pick_host_port 重映射）：
#    若 redis/pg read-back ≠ 默认值（6379/5432），说明 host 端口被占、compose 重映射了，
#    而 git-truth ConfigMap 仍是默认端口 → pod 会连不上 → 需单独的运行时端口注入方案
#    （见 .superpowers/sdd/task-2-fix-report.md 的 concern）。Kafka 固定 9094 不重映射。
read_compose_host_port() {  # NAME CONTAINER_PORT -> 实际 publish 的 host 端口
  docker port "$1" "$2" 2>/dev/null | awk -F: '/^0\.0\.0\.0:/ {print $NF; exit}'
}
REDIS_HP=$(read_compose_host_port apihub-redis 6379)
PG_HP=$(read_compose_host_port apihub-pg 5432)
MINIO_HP=$(read_compose_host_port apihub-minio 9000)
echo "compose publish host ports (read back, diagnostic): redis=$REDIS_HP pg=$PG_HP minio=$MINIO_HP  (kafka fixed 9094)"
if [ "${REDIS_HP:-}" != "6379" ] || [ "${PG_HP:-}" != "5432" ]; then
  echo "WARN: compose 重映射了 redis/pg 端口（host 被占），但 git-truth ConfigMap 用默认端口 →" >&2
  echo "      pod 经 host.docker.internal 连 PG/Redis 会失败。需运行时端口注入（见 task-2-fix-report）。" >&2
fi

# 4) 构建 11 镜像 + load 进 kind
SVC=(api-registry dispatcher auth executor quota tenant admin docs trace retry workflow portal notification ai-gateway billing)
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

# 5a) shared-infra（数据层连接 CM/Secret）—— 故意不在 kustomization resources（避 ArgoCD
#     管，见 shared-infra.yaml 顶部注释：由独立 kubectl apply 下发）。bootstrap 需自己 apply，
#     否则 deployment envFrom 的 apihub-shared-infra 缺失 → 全员 CreateContainerConfigError。
#     端口用 compose 实际 publish（上方 read-back 的 PG_HP/REDIS_HP）patch 进 live CM——
#     host 5432/6379 被占时 compose 重映射到 15433/16379 等，git 模板里是标准端口。
kubectl apply -f deploy/k8s/overlays/kind/shared-infra.yaml
kubectl -n apihub-system patch cm apihub-shared-infra --type merge \
  -p "{\"data\":{\"PG_PORT\":\"$PG_HP\",\"REDIS_PORT\":\"$REDIS_HP\"}}"

# 5b) 注入 host.docker.internal → docker-bridge IP 的集群范围 DNS 解析（CoreDNS 运行时层）。
#     kind-on-Linux pod 不继承 node /etc/hosts → host.docker.internal 默认不可解析；
#     CoreDNS 在 kube-system（不在 overlay 目录），ArgoCD GitOps 管不到 → 注入不会被 selfHeal 回滚。
bash scripts/k8s/patch-coredns-hosts.sh

# 6) 等 ready（PSA restricted 下可能因 seccompProfile/securityContext 缺失而被拒，
#    届时 kubectl wait 会超时——参见日志中 ReplicaSet FailedCreate 事件）
kubectl wait --for=condition=ready pods -n apihub-system --all --timeout=300s

# 7) 健康抽检
kubectl -n apihub-system port-forward svc/api-registry 18000:80 &
PFS=$!
sleep 3
curl -sf http://127.0.0.1:18000/health/ready && echo " <- api-registry ready"
kill $PFS 2>/dev/null || true

# 8) 装 Argo Workflow + 配 MinIO artifactRepository（真 Argo e2e）
echo "=== argo-setup (real Argo) ==="
bash scripts/kind/argo-setup.sh

# workflow pod 须以 argo_mode=k8s 重建（overlay 已把 configmap 改 k8s）
kubectl -n apihub-system rollout restart deploy/workflow
kubectl -n apihub-system wait deploy/workflow --for=condition=Available --timeout=120s

echo "DONE: kind stack up + real Argo. host_ip=$HOST_IP redis=$REDIS_HP pg=$PG_HP minio=$MINIO_HP"
