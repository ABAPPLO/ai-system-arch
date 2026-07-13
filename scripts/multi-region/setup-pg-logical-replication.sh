#!/bin/bash
# Setup per-tenant PG logical replication (bidirectional, origin=none)
# Usage: setup-pg-logical-replication.sh <tenant_id> <home_region>
set -euo pipefail

TENANT_ID=$1; HOME_REGION=$2

if [ "$HOME_REGION" = "sh" ]; then
  PRIMARY_DSN=$PG_DSN_SH; STANDBY_DSN=$PG_DSN_BJ
  PUB_NAME="pub_tenant_${TENANT_ID}_sh"; SUB_NAME="sub_tenant_${TENANT_ID}_sh"
elif [ "$HOME_REGION" = "bj" ]; then
  PRIMARY_DSN=$PG_DSN_BJ; STANDBY_DSN=$PG_DSN_SH
  PUB_NAME="pub_tenant_${TENANT_ID}_bj"; SUB_NAME="sub_tenant_${TENANT_ID}_bj"
else
  echo "Invalid region: $HOME_REGION"; exit 1
fi

psql "$PRIMARY_DSN" <<SQL
  DROP PUBLICATION IF EXISTS ${PUB_NAME};
  CREATE PUBLICATION ${PUB_NAME} FOR ALL TABLES;
SQL

psql "$STANDBY_DSN" <<SQL
  DROP SUBSCRIPTION IF EXISTS ${SUB_NAME};
  CREATE SUBSCRIPTION ${SUB_NAME}
    CONNECTION '${PRIMARY_DSN}'
    PUBLICATION ${PUB_NAME}
    WITH (origin = none);
SQL

sleep 2
psql "$STANDBY_DSN" -c "SELECT pid, state, replay_lag FROM pg_stat_wal_receiver;"
