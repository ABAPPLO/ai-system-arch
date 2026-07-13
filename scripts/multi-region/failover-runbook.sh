#!/bin/bash
set -euo pipefail
FAILED_REGION=$1; DRY_RUN="${2:-}"

if [ "$FAILED_REGION" = "sh" ]; then SURVIVING_REGION="bj"; SURVIVING_PG_DSN=$PG_DSN_BJ
elif [ "$FAILED_REGION" = "bj" ]; then SURVIVING_REGION="sh"; SURVIVING_PG_DSN=$PG_DSN_SH
else echo "Invalid region: $FAILED_REGION"; exit 1; fi

echo "[1/5] Health check — surviving region"
curl -sf "http://${SURVIVING_REGION}-gw:8001/health/ready" || { echo "Surviving region unhealthy"; exit 1; }

echo "[2/5] PG — disable subscriptions + move tenants"
for tid in $(psql "$SURVIVING_PG_DSN" -Atc "SELECT id FROM tenant WHERE home_region='$FAILED_REGION'"); do
  if [ "$DRY_RUN" != "--dry-run" ]; then
    psql "$SURVIVING_PG_DSN" -c "ALTER SUBSCRIPTION sub_tenant_${tid}_${FAILED_REGION} DISABLE;"
    psql "$SURVIVING_PG_DSN" -c "UPDATE tenant SET home_region='$SURVIVING_REGION' WHERE id=$tid;"
  else echo "[DRY-RUN] Would move tenant $tid to $SURVIVING_REGION"; fi
done

echo "[3/5] DNS switch — manual step"
echo "  aliyun alidns UpdateDomainRecord --RecordId <id> --RR api --Type A --Value <${SURVIVING_REGION}-slb-ip> --TTL 30"
echo "[4/5] Verify"; curl -sf "http://${SURVIVING_REGION}-gw:8001/health/ready"
echo "[5/5] Done — failover to $SURVIVING_REGION complete"
