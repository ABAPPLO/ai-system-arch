#!/bin/bash
set -euo pipefail
KAFKA_SH="${KAFKA_SH:-kafka-sh-1:9092,kafka-sh-2:9092,kafka-sh-3:9092}"
KAFKA_BJ="${KAFKA_BJ:-kafka-bj-1:9092,kafka-bj-2:9092,kafka-bj-3:9092}"
TOPICS="${TOPICS:-api-call-events,task-requests,task-failures,audit-events,billing-events}"

echo "Starting MirrorMaker: sh → bj"
docker run -d --name mirrormaker-sh2bj --restart unless-stopped \
  confluentinc/cp-kafka:latest \
  /usr/bin/kafka-mirror-maker \
  --consumer.config <(echo -e "bootstrap.servers=$KAFKA_SH\ngroup.id=mirrormaker-sh2bj") \
  --producer.config <(echo -e "bootstrap.servers=$KAFKA_BJ") \
  --whitelist="$TOPICS"

echo "Starting MirrorMaker: bj → sh"
docker run -d --name mirrormaker-bj2sh --restart unless-stopped \
  confluentinc/cp-kafka:latest \
  /usr/bin/kafka-mirror-maker \
  --consumer.config <(echo -e "bootstrap.servers=$KAFKA_BJ\ngroup.id=mirrormaker-bj2sh") \
  --producer.config <(echo -e "bootstrap.servers=$KAFKA_SH") \
  --whitelist="$TOPICS"

echo "Done. Verify: kafka-console-consumer ... --from-beginning --max-messages 1"
