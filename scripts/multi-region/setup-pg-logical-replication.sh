#!/bin/bash
# 全库双向逻辑订阅（origin=none 防回环）。正确性承赖写分区：每行只在其 home_region 写。
# 无冲突前提由 S1（APISIX 写亲和）保证。用法：PG_DSN_SH=... PG_DSN_BJ=... ./setup-pg-logical-replication.sh
set -euo pipefail

: "${PG_DSN_SH:?PG_DSN_SH required}"
: "${PG_DSN_BJ:?PG_DSN_BJ required}"

require_pg16() { # $1 = dsn
  local major
  major=$(psql "$1" -Atc "SELECT current_setting('server_version_num')::int / 10000")
  [ "$major" -ge 16 ] || { echo "FAIL: PG>=16 required (got $major.x) for origin=none" >&2; exit 1; }
}
require_logical() { # $1 = dsn
  local wl; wl=$(psql "$1" -Atc "SHOW wal_level")
  [ "$wl" = "logical" ] || { echo "FAIL: wal_level=logical required (got $wl)" >&2; exit 1; }
}

echo "[pre] version + wal_level checks"
require_pg16 "$PG_DSN_SH"; require_pg16 "$PG_DSN_BJ"
require_logical "$PG_DSN_SH"; require_logical "$PG_DSN_BJ"

setup_direction() { # $1=src_dsn $2=dst_dsn $3=src_region $4=dst_region
  local SRC_DSN="$1" DST_DSN="$2" SRC="$3" DST="$4"
  local PUB="pub_all_${SRC}" SUB="sub_from_${SRC}_on_${DST}"
  echo "[dir] ${SRC} -> ${DST}  (${PUB} / ${SUB})"
  psql "$SRC_DSN" <<SQL
    DO \$\$ BEGIN
      IF NOT EXISTS (SELECT 1 FROM pg_publication WHERE pubname='${PUB}') THEN
        CREATE PUBLICATION ${PUB} FOR ALL TABLES;
      END IF;
    END \$\$;
SQL
  psql "$DST_DSN" <<SQL
    DO \$\$ BEGIN
      IF NOT EXISTS (SELECT 1 FROM pg_subscription WHERE subname='${SUB}') THEN
        EXECUTE 'CREATE SUBSCRIPTION ${SUB} CONNECTION ''${SRC_DSN}'' PUBLICATION ${PUB} WITH (copy_data = true, create_slot = true, enabled = true, origin = none)';
      END IF;
    END \$\$;
SQL
}

setup_direction "$PG_DSN_SH" "$PG_DSN_BJ" sh bj
setup_direction "$PG_DSN_BJ" "$PG_DSN_SH" bj sh

echo "[done] bidirectional logical replication established (origin=none)"
