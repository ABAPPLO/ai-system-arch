#!/usr/bin/env bash
# 初始化 Kafka topic —— 与 docs/04-data-model.md §6 对齐
# 与 deploy/terraform/modules/kafka/variables.tf 中的默认 topic 列表保持一致

set -euo pipefail

BROKER="${KAFKA_BOOTSTRAP:-kafka:9092}"

# name:partitions:replicas:retention_hours
TOPICS=(
    "api-call-events:12:1:168"      # 调用事件（保留 7 天）
    "task-requests:6:1:72"          # 异步任务请求
    "task-status:6:1:72"            # 任务状态变更
    "retry-requests:6:1:72"         # 重试队列
    "audit-events:3:1:720"          # 审计（保留 30 天，落 PG 后归档）
    "notification:3:1:168"          # 通知（钉钉/邮件/SMS/Webhook）
)

echo "==> Kafka broker: $BROKER"
echo "==> Creating ${#TOPICS[@]} topics..."

for entry in "${TOPICS[@]}"; do
    name="${entry%%:*}"
    rest="${entry#*:}"
    partitions="${rest%%:*}"
    rest="${rest#*:}"
    replicas="${rest%%:*}"
    retention="${rest##*:}"

    echo "--- $name (partitions=$partitions, replicas=$replicas, retention=${retention}h)"

    kafka-topics.sh --bootstrap-server "$BROKER" \
        --create --if-not-exists \
        --topic "$name" \
        --partitions "$partitions" \
        --replication-factor "$replicas" \
        --config "retention.ms=$((retention * 3600 * 1000))"

    # 强制确保至少一个分区可用（KRaft 单节点 replicas=1）
    kafka-topics.sh --bootstrap-server "$BROKER" --describe --topic "$name"
done

echo "==> Topics ready:"
kafka-topics.sh --bootstrap-server "$BROKER" --list
