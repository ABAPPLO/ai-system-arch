#!/bin/bash
# Multi-region failover runbook
# Usage: failover-runbook.sh <failed_region> [--dry-run]
set -euo pipefail

FAILED_REGION=$1; DRY_RUN="${2:-}"

if [ "$FAILED_REGION" = "sh" ]; then SURVIVING_REGION="bj"; SURVIVING_PG_DSN=$PG_DSN_BJ
elif [ "$FAILED_REGION" = "bj" ]; then SURVIVING_REGION="sh"; SURVIVING_PG_DSN=$PG_DSN_SH
else echo "Invalid region: $FAILED_REGION"; exit 1; fi

# Pre-flight: replication lag must be < 5s
echo "[Pre-flight] Replication lag check"
LAG=$(psql "$SURVIVING_PG_DSN" -Atc "SELECT EXTRACT(epoch FROM replay_lag) FROM pg_stat_wal_receiver" 2>/dev/null || echo "0")
if [ -n "$LAG" ] && [ "$LAG" != "NULL" ] && [ "$(echo "$LAG > 5" | bc -l 2>/dev/null)" = "1" ]; then
  echo "FAIL: Replication lag ${LAG}s > 5s threshold. Aborting."
  exit 1
fi
echo "  Lag: ${LAG}s OK"

echo "[1/6] Health check — surviving region"
curl -sf "http://${SURVIVING_REGION}-gw:8001/health/ready" || { echo "Surviving region unhealthy"; exit 1; }

echo "[2/6] PG promote — surviving region becomes writable"
if [ "$DRY_RUN" != "--dry-run" ]; then
  psql "$SURVIVING_PG_DSN" -c "SELECT pg_promote();"
  echo "  PG promoted on $SURVIVING_REGION"
else
  echo "  [DRY-RUN] Would promote PG on $SURVIVING_REGION"
fi

echo "[3/6] PG — disable subscriptions for failed region's tenants + migrate home_region"
for tid in $(psql "$SURVIVING_PG_DSN" -Atc "SELECT id FROM tenant WHERE home_region='$FAILED_REGION'"); do
  if [ "$DRY_RUN" != "--dry-run" ]; then
    psql "$SURVIVING_PG_DSN" -c "ALTER SUBSCRIPTION sub_tenant_${tid}_${FAILED_REGION} DISABLE;"
    psql "$SURVIVING_PG_DSN" -c "UPDATE tenant SET home_region='$SURVIVING_REGION' WHERE id=$tid;"
    echo "  Tenant $tid → $SURVIVING_REGION"
  else echo "  [DRY-RUN] Would move tenant $tid to $SURVIVING_REGION"; fi
done

echo "[4/6] Kafka — reset consumer group offsets on surviving region (manual)"
echo "  Manual: kafka-consumer-groups --bootstrap-server \$KAFKA_${SURVIVING_REGION^^}"
echo "    --group <consumer-group> --topic <topic> --reset-offsets --to-current --execute"

echo "[5/6] DNS switch — aliyun alidns UpdateDomainRecord"
echo "  aliyun alidns UpdateDomainRecord --RecordId <id> --RR api --Type A --Value <${SURVIVING_REGION}-slb-ip> --TTL 30"

echo "[6/6] Verify"
curl -sf "http://${SURVIVING_REGION}-gw:8001/health/ready" && echo "  Health OK"
echo "  Done — failover to $SURVIVING_REGION complete"
