#!/bin/bash
set -euo pipefail
# Region B (cn-beijing) health check for GSLB

HEALTH_URL="${1:-http://localhost:8001/health/ready}"
PG_DSN="${2:-}"

# 1. Gateway health check
curl -sf -o /dev/null "$HEALTH_URL" || exit 1

# 2. PG replication lag check (if connection string provided)
if [ -n "$PG_DSN" ]; then
  LAG=$(psql "$PG_DSN" -Atc "SELECT EXTRACT(epoch FROM replay_lag) FROM pg_stat_wal_receiver" 2>/dev/null || echo "0")
  if [ -n "$LAG" ] && [ "$LAG" != "NULL" ] && [ "$(echo "$LAG > 30" | bc -l 2>/dev/null)" = "1" ]; then
    echo "PG replication lag > 30s: ${LAG}s"
    exit 1
  fi
fi

echo "OK"
exit 0
