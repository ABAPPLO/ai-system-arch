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

setup_direction() { # $1=src_dsn $2=dst_dsn $3=src_dsn_internal $4=src_region $5=dst_region
  local SRC_DSN="$1" DST_DSN="$2" SRC_DSN_INTERNAL="$3" SRC="$4" DST="$5"
  local PUB="pub_all_${SRC}" SUB="sub_from_${SRC}_on_${DST}"
  echo "[dir] ${SRC} -> ${DST}  (${PUB} / ${SUB})"
  # CREATE PUBLICATION 可在 DO 块内幂等创建
  psql "$SRC_DSN" -c "DO \$\$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_publication WHERE pubname='${PUB}') THEN CREATE PUBLICATION ${PUB} FOR ALL TABLES; END IF; END \$\$;"
  # CREATE SUBSCRIPTION ... WITH (create_slot=true) 不能在函数/事务块内执行（PG 限制），
  # 故先查存在性，再以单语句 autocommit 方式创建（psql -c 默认每条 autocommit）。
  # CONNECTION 串在 DST 容器内执行，必须用 docker 网络内地址（src_internal），不能用 host 映射端口。
  local exists
  exists=$(psql "$DST_DSN" -Atc "SELECT 1 FROM pg_subscription WHERE subname='${SUB}'" 2>/dev/null || true)
  if [ "$exists" = "1" ]; then
    echo "[dir] ${SUB} already exists, skip create"
  else
    psql "$DST_DSN" -c "CREATE SUBSCRIPTION ${SUB} CONNECTION '${SRC_DSN_INTERNAL}' PUBLICATION ${PUB} WITH (copy_data = true, create_slot = true, enabled = true, origin = none);"
  fi
}

: "${PG_DSN_SH_INTERNAL:=$PG_DSN_SH}"
: "${PG_DSN_BJ_INTERNAL:=$PG_DSN_BJ}"

setup_direction "$PG_DSN_SH" "$PG_DSN_BJ" "$PG_DSN_SH_INTERNAL" sh bj
setup_direction "$PG_DSN_BJ" "$PG_DSN_SH" "$PG_DSN_BJ_INTERNAL" bj sh

echo "[done] bidirectional logical replication established (origin=none)"
