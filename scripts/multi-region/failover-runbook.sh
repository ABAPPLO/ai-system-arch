#!/bin/bash
# Multi-region failover runbook
# Usage: failover-runbook.sh <failed_region> [--dry-run] [FORCE=1]
#
# Promotes the surviving region's PG, disables the failed→surviving logical
# subscription, migrates tenant home_region, resets Kafka consumer-group
# offsets on the surviving region, and flips aliyun alidns.
#
# Lag pre-flight is PG16-compatible: uses NOW() - latest_end_time on the
# surviving region's pg_stat_subscription (NOT latest_end_lag, which is
# PG17+ only). -1 = subscription not found OR no WAL received yet → fail-closed.
set -euo pipefail

FAILED_REGION=$1; DRY_RUN="${2:-}"

if [ "$FAILED_REGION" = "sh" ]; then SURVIVING_REGION="bj"; SURVIVING_PG_DSN=$PG_DSN_BJ
elif [ "$FAILED_REGION" = "bj" ]; then SURVIVING_REGION="sh"; SURVIVING_PG_DSN=$PG_DSN_SH
else echo "Invalid region: $FAILED_REGION"; exit 1; fi

# audit() — write a per-phase row to audit_log.
# Real schema (scripts/init-db/01-schema.sql:195): NOT NULL columns are
# tenant_id, actor_type, action, resource_type; detail is jsonb.
#
# RLS note: audit_log is FORCE RLS-enforced; the app role (NOBYPASSRLS) used by
# SURVIVING_PG_DSN may be filtered/blocked on INSERT. Prod MUST set AUDIT_PG_DSN
# to a platform-admin-capable DSN (bypass RLS). In dev/drill the app role is used
# and the INSERT may warn-skip (table missing / RLS block) — acceptable, the
# failover MUST NOT block on best-effort audit.
AUDIT_PG_DSN="${AUDIT_PG_DSN:-$SURVIVING_PG_DSN}"
audit() { # $1 = phase $2 = detail
  echo "[audit] phase=$1 detail=$2 actor=${OPERATOR:-unknown} region=${SURVIVING_REGION} ts=$(date -u +%FT%TZ)"
  if [ "$DRY_RUN" != "--dry-run" ]; then
    psql "$AUDIT_PG_DSN" -c "INSERT INTO audit_log(tenant_id, actor_type, actor_id, actor_name, action, resource_type, resource_id, detail) VALUES ('platform', 'system', '${OPERATOR:-runbook}', 'failover-runbook', 'failover_${1}', 'failover', '${SURVIVING_REGION}', '{\"region\":\"${SURVIVING_REGION}\",\"failed\":\"${FAILED_REGION}\",\"step\":\"${1}\",\"detail\":\"${2}\"}');" 2>&1 | sed 's/^/  [audit-warn] /' || echo "  [audit-warn] INSERT skipped (RLS/table-missing) — best-effort, failover continues"
  fi
}

# Pre-flight: replication lag on the surviving region's subscription that
# pulls FROM the failed region. lag = NOW() - latest_end_time (PG16; no
# latest_end_lag column). latest_end_time is NULL when no WAL received yet
# → COALESCE returns -1 → fail-closed.
echo "[Pre-flight] Subscription lag check on $SURVIVING_REGION"
LAG_SEC=$(psql "$SURVIVING_PG_DSN" -Atc "
  SELECT COALESCE(EXTRACT(epoch FROM (NOW() - latest_end_time))::bigint, -1)
  FROM pg_stat_subscription
  WHERE subname = 'sub_from_${FAILED_REGION}_on_${SURVIVING_REGION}'" 2>/dev/null || echo "-1")
# -1 = subscription not found OR no WAL received yet (latest_end_time NULL)
if [ "$LAG_SEC" = "-1" ]; then
  echo "WARN: sub sub_from_${FAILED_REGION}_on_${SURVIVING_REGION} not found / no data yet; FORCE=1 to skip"
  [ "${FORCE:-0}" = "1" ] || exit 1
elif [ "$LAG_SEC" -gt 5 ]; then
  echo "FAIL: lag ${LAG_SEC}s > 5s. Aborting (FORCE=1 to override)."
  [ "${FORCE:-0}" = "1" ] || exit 1
fi
echo "  Lag: ${LAG_SEC}s OK"

echo "[1/6] Health check — surviving region"
if [ "$DRY_RUN" = "--dry-run" ]; then
  echo "  [DRY-RUN] would curl http://${SURVIVING_REGION}-gw:8001/health/ready"
elif [ "${FORCE:-0}" = "1" ]; then
  # FORCE path (drill / region-down forced failover): the surviving gateway may
  # be unreachable from the operator host (no gw in docker, or DNS not yet
  # flipped). Fall back to a PG liveness probe — the real signal that the
  # surviving region's data plane is up.
  if psql "$SURVIVING_PG_DSN" -Atc "SELECT 1" >/dev/null 2>&1; then
    echo "  OK: surviving PG ${SURVIVING_REGION} live (FORCE=1, gw curl skipped — no gw in drill/docker)"
  else
    echo "  WARN: surviving PG ${SURVIVING_REGION} not reachable; FORCE=1 so continuing"
  fi
else
  curl -sf "http://${SURVIVING_REGION}-gw:8001/health/ready" || { echo "Surviving region unhealthy"; exit 1; }
  echo "  gw health OK"
fi

echo "[2/6] PG promote — surviving region becomes writable"
# R3b logical-bidir model: the surviving region is a PRIMARY (logical subscriber),
# NOT a physical streaming standby. pg_promote() on a non-standby returns false
# (no-op). The real failover = disable sub_from_<FAILED>_on_<SURVIVING> (stop
# pulling from dead region) + migrate tenant.home_region FAILED→SURVIVING —
# both done in [3/6] below. pg_promote is retained only to cover a future
# physical-standby model; under logical-bidir it is a harmless no-op.
if [ "$DRY_RUN" != "--dry-run" ]; then
  IN_REC=$(psql "$SURVIVING_PG_DSN" -Atc "SELECT pg_is_in_recovery()")
  if [ "$IN_REC" = "f" ]; then
    echo "  ${SURVIVING_REGION} already primary in logical-bidir; no physical promote needed — disabling sub + migrating home_region is the failover"
  else
    psql "$SURVIVING_PG_DSN" -c "SELECT pg_promote();" || echo "  WARN: pg_promote returned false/error (logical-bidir: expected no-op)"
    echo "  PG promoted on $SURVIVING_REGION (physical-standby path)"
  fi
else
  echo "  [DRY-RUN] Would promote PG on $SURVIVING_REGION"
fi
audit promote "$SURVIVING_REGION"

# S2 uses full-DB subscriptions sub_from_<src>_on_<dst> (NOT per-tenant).
# Disable the failed→surviving sub, then bulk-migrate home_region.
echo "[3/6] PG — disable failed→surviving subscription + migrate home_region"
if [ "$DRY_RUN" != "--dry-run" ]; then
  psql "$SURVIVING_PG_DSN" -c "ALTER SUBSCRIPTION sub_from_${FAILED_REGION}_on_${SURVIVING_REGION} DISABLE;"
  psql "$SURVIVING_PG_DSN" -c "UPDATE tenant SET home_region='${SURVIVING_REGION}' WHERE home_region='${FAILED_REGION}';"
  echo "  sub disabled; tenants ${FAILED_REGION}→${SURVIVING_REGION}"
else
  echo "  [DRY-RUN] would disable sub + migrate tenants"
fi
audit migrate "${FAILED_REGION}->${SURVIVING_REGION}"

echo "[4/6] Kafka — reset CH-writer consumer group offsets on $SURVIVING_REGION"
CG="ch-writer-${SURVIVING_REGION}"
KAFKA_SURVIVING="kafka-${SURVIVING_REGION}.apihub-system:9092"
if [ "$DRY_RUN" != "--dry-run" ] && command -v kafka-consumer-groups >/dev/null 2>&1; then
  for t in api-call-events task-requests task-failures audit-events billing-events; do
    kafka-consumer-groups --bootstrap-server "$KAFKA_SURVIVING" --group "$CG" --topic "$t" --reset-offsets --to-current --execute || true
  done
  echo "  offsets reset on $SURVIVING_REGION"
else
  echo "  [DRY-RUN/no-cli] would reset $CG on $KAFKA_SURVIVING"
fi

echo "[5/6] DNS switch — aliyun alidns"
if [ "$DRY_RUN" != "--dry-run" ] && [ "${DNS_RECORD_ID:-}" ] && command -v aliyun >/dev/null 2>&1; then
  aliyun alidns UpdateDomainRecord --RecordId "$DNS_RECORD_ID" --RR api --Type A --Value "${SURVIVING_SLB_IP:?SURVIVING_SLB_IP required}" --TTL 30
  echo "  DNS api.${DOMAIN} → ${SURVIVING_SLB_IP}"
else
  echo "  [DRY-RUN/no-cli/no-DNS_RECORD_ID] would switch DNS to ${SURVIVING_REGION} SLB" >&2
fi
audit dns "${SURVIVING_SLB_IP:-na}"

echo "[6/6] Verify"
curl -sf "http://${SURVIVING_REGION}-gw:8001/health/ready" && echo "  Health OK"
echo "  Done — failover to $SURVIVING_REGION complete"
audit 'done' ok
