#!/bin/bash
# 真入口驱动（审计 §6）：双 Kafka MM2 双向 + IdentityReplicationPolicy 无重复 e2e。
# 前置：docker compose -f docker-compose.multi-region.yml up -d kafka-sh kafka-bj
# （S3-T1 的 MM2 k8s Deployment 不在 kind kustomization —— 单 Kafka kind 会让 MM2 crash-loop，
#  故 e2e 用本地 docker MM2 对 2 个 docker-compose Kafka 实例）。
#
# 断言：
#   - sh → bj 投递（count==3）
#   - bj → sh 投递（count==3）
#   - 无回环放大（count==produced，非 2×）  ← IdentityReplicationPolicy 保持 topic 名不变，
#     避免 DefaultMirrorPolicy 的 "sh.api-call-events" 重命名 → 反向再镜像 → 循环放大。
set -euo pipefail
cd "$(dirname "$0")/../.."

# --- KIMAGE：自动探测本地 kafka 镜像（避免 docker pull 摩擦）---
KIMAGE="${KIMAGE:-$(docker images --format '{{.Repository}}:{{.Tag}}' \
  | grep -iE 'bitnami|bitnamilegacy' | grep -i kafka | head -1)}"
if [ -z "${KIMAGE:-}" ]; then
  KIMAGE="bitnami/kafka:3.7"  # fallback（可能 pull 失败 → 视为 BLOCKER）
fi
echo "KIMAGE=$KIMAGE"
if ! docker image inspect "$KIMAGE" >/dev/null 2>&1; then
  echo "BLOCKER: kafka image $KIMAGE not present locally and pull is gated by host socks5 proxy"
  exit 2
fi

BIN=/opt/bitnami/kafka/bin  # bitnami + bitnamilegacy 同路径
TOPIC=api-call-events
GRP_SH="e2e_mm2_$$_sh"
GRP_BJ="e2e_mm2_$$_bj"

# --- 1. 起双 Kafka（幂等）---
docker compose -f docker-compose.multi-region.yml up -d kafka-sh kafka-bj
sleep 5  # 等 KRaft controller 就绪

# 探活 + 重建测试 topic（先删后建 → 干净起点，避免上一轮回环遗留的 986K 条污染 end-offset）
for port in 9092 9093; do
  for _ in $(seq 1 20); do
    if docker run --rm --network host "$KIMAGE" \
        "$BIN/kafka-topics.sh" --bootstrap-server localhost:$port \
        --list 2>/dev/null | grep -qx "$TOPIC"; then
      break
    fi
    sleep 1
  done
  docker run --rm --network host "$KIMAGE" \
    "$BIN/kafka-topics.sh" --bootstrap-server localhost:$port \
    --delete --topic "$TOPIC" >/dev/null 2>&1 || true
  sleep 1
  docker run --rm --network host "$KIMAGE" \
    "$BIN/kafka-topics.sh" --bootstrap-server localhost:$port \
    --create --topic "$TOPIC" --partitions 1 --replication-factor 1 \
    >/dev/null 2>&1
  echo "OK: topic $TOPIC recreated on :$port"
done

# --- 2. 写本地 MM2 配置 ---
cat > /tmp/mm2-e2e.properties <<'EOF'
clusters = sh, bj
sh.bootstrap.servers = localhost:9092
bj.bootstrap.servers = localhost:9093
replication.policy.class = org.apache.kafka.connect.mirror.IdentityReplicationPolicy
sh->bj.enabled = true
sh->bj.topics = api-call-events
bj->sh.enabled = true
bj->sh.topics = api-call-events
emit.heartbeats.enabled = false
sync.group.offsets.enabled = false
# 单 broker 集群：所有内部 topic RF 必须降到 1（默认 3 在 1-broker 集群会 RF 不足 →
# TopicAdmin 持续创建失败 → MM2 herder TimeoutException 退出）
replication.factor = 1
checkpoints.topic.replication.factor = 1
heartbeats.topic.replication.factor = 1
offset-syncs.topic.replication.factor = 1
offset.storage.replication.factor = 1
config.storage.replication.factor = 1
status.storage.replication.factor = 1
EOF

# --- 3. 起本地 docker MM2（host network，localhost:9092/9093 可达）---
docker rm -f mm2-e2e >/dev/null 2>&1 || true
docker run -d --name mm2-e2e --network host \
  -v /tmp/mm2-e2e.properties:/mm2.properties \
  "$KIMAGE" "$BIN/connect-mirror-maker.sh" /mm2.properties
# 等待 MM2 完成 connector 初始化（~15s）。若容器已退出 → 启动失败，dump 日志。
for _ in $(seq 1 30); do
  if ! docker ps --format '{{.Names}}' | grep -q '^mm2-e2e$'; then
    echo "BLOCKER: mm2-e2e container exited during startup — dumping logs:"
    docker logs mm2-e2e 2>&1 | tail -30
    exit 3
  fi
  docker logs mm2-e2e 2>&1 | grep -qiE \
    'MirrorSourceConnector|Creating connector|Starting.*connector|Connector lifecycle' && break
  sleep 1
done
sleep 5

# --- 4. 辅助：取 topic 的 log-end-offset（确定性，不消费 90K 条）---
get_end_offset() {
  local port="$1"
  docker run --rm --network host "$KIMAGE" \
    "$BIN/kafka-get-offsets.sh" --bootstrap-server "localhost:$port" \
    --topic "$TOPIC" --time -1 2>/dev/null \
    | awk -F: '{sum+=$NF} END{print sum+0}'
}

# verdict(produced, observed, direction)
#   observed == produced        → PASS
#   observed == 0               → NO_DELIVERY（MM2 没投递）
#   observed > produced         → LOOP_AMPLIFICATION（IdentityReplicationPolicy 双向同 topic 回环）
verdict() {
  local produced="$1" observed="$2" dir="$3"
  if [ "${observed:-0}" -eq "${produced}" ]; then
    echo "OK $dir: end-offset=$observed == produced=$produced (no-dup)"
    return 0
  elif [ "${observed:-0}" -eq 0 ]; then
    echo "FAIL $dir: NO_DELIVERY (end-offset=0, expected $produced) — MM2 未投递"
    return 1
  elif [ "${observed:-0}" -gt "${produced}" ]; then
    echo "FAIL $dir: LOOP_AMPLIFICATION (end-offset=$observed >> produced=$produced)"
    echo "  >>> 双向 MM2 + IdentityReplicationPolicy + 同名 topic = 回环放大。"
    echo "  >>> IdentityReplicationPolicy 仅防 topic 改名（不防 record 级回环）；"
    echo "  >>> offset-sync 源集群溯源在同名 topic 下失效，bj->sh 把 sh->bj 刚镜像回的记录再镜像回去。"
    echo "  >>> 修复方向：DefaultMirrorPolicy（rename 防 back-mirror）或非对称 topic 过滤或单方向。"
    return 2
  else
    echo "FAIL $dir: end-offset=$observed (expected $produced) — 欠投递"
    return 1
  fi
}

# --- 5. sh→bj：produce 3 到 sh，等 MM2，读 bj 的 log-end-offset ---
PRODUCED_SH=3
for i in $(seq 1 "$PRODUCED_SH"); do
  echo "msg-$i" | docker run -i --rm --network host "$KIMAGE" \
    "$BIN/kafka-console-producer.sh" --bootstrap-server localhost:9092 --topic "$TOPIC" \
    >/dev/null 2>&1
done
echo "produced $PRODUCED_SH msgs to sh:$TOPIC"
sleep 6  # 等 MM2 跨集群复制 + 任何回环稳定
BJ_OFF=$(get_end_offset 9093)
SH_OFF_AFTER=$(get_end_offset 9092)
echo "end-offsets: sh=$SH_OFF_AFTER  bj=$BJ_OFF (produced-to-sh=$PRODUCED_SH)"
verdict "$PRODUCED_SH" "$BJ_OFF" "sh→bj" || RC=$?
# 同时确认 bj 上确有内容（读 max 6 条 sample，证明非空跑）
SAMPLE_BJ=$(timeout 10 docker run --rm --network host "$KIMAGE" \
  "$BIN/kafka-console-consumer.sh" --bootstrap-server localhost:9093 --topic "$TOPIC" \
  --from-beginning --max-messages 6 --group "$GRP_BJ" 2>/dev/null \
  | grep -c '^msg-' || true)
echo "sample-from-bj: $SAMPLE_BJ msgs (max 6) — proves delivery not empty"

# --- 6. 反向 bj→sh：produce 3 到 bj，等 MM2，读 sh 的 log-end-offset ---
#   （若 5 已触发回环，sh 的 end-offset 早已远超 3 —— 这正是回环证据。）
PRODUCED_BJ=3
for i in 4 5 6; do
  echo "msg-$i" | docker run -i --rm --network host "$KIMAGE" \
    "$BIN/kafka-console-producer.sh" --bootstrap-server localhost:9093 --topic "$TOPIC" \
    >/dev/null 2>&1
done
echo "produced $PRODUCED_BJ msgs to bj:$TOPIC"
sleep 6
SH_OFF=$(get_end_offset 9092)
BJ_OFF_AFTER=$(get_end_offset 9093)
echo "end-offsets: sh=$SH_OFF  bj=$BJ_OFF_AFTER (produced-to-bj=$PRODUCED_BJ, cumulative-sh-input=$((PRODUCED_SH+PRODUCED_BJ)))"
# 双向 no-dup 判据：sh 累计直写 = PRODUCED_SH+PRODUCED_BJ = 6；若 sh end-offset==6 则无回环。
verdict $((PRODUCED_SH+PRODUCED_BJ)) "$SH_OFF" "bj→sh" || RC=$?

# --- 7. 结论 ---
RC=${RC:-0}
docker rm -f mm2-e2e >/dev/null 2>&1 || true
if [ "$RC" -eq 0 ]; then
  echo "e2e-mm2 PASS"
else
  echo "e2e-mm2 FAIL (rc=$RC) — 见上方 verdict；若为 LOOP_AMPLIFICATION 则是真实 MM2 双向 IdentityReplicationPolicy 回环 bug"
fi
exit "$RC"
