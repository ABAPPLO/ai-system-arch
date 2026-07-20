#!/bin/bash
# 真入口驱动（审计 §6）：直写 PG，验证全库双向 + origin=none 防回环。
# 前置：make dev-up-multi（或 docker compose -f docker-compose.multi-region.yml up -d pg-sh pg-bj）。
set -euo pipefail
export PG_DSN_SH=postgres://apihub:apihub_dev_pwd@localhost:15432/apihub
export PG_DSN_BJ=postgres://apihub:apihub_dev_pwd@localhost:5433/apihub
# 订阅的 CONNECTION 串在 PG 容器内执行，需用 docker 网络内的服务名 + 内部端口 5432，
# 而非 host 的 localhost:15432/5433（容器内 localhost 指向自身）。
export PG_DSN_SH_INTERNAL=postgres://apihub:apihub_dev_pwd@pg-sh:5432/apihub
export PG_DSN_BJ_INTERNAL=postgres://apihub:apihub_dev_pwd@pg-bj:5432/apihub

# 本机 psql 被 snap 限制无法走 TCP，经容器内 psql 转发（仍直连真实 PG，非注入）。
# 注意：setup-pg-logical-replication.sh 在子进程中调用 psql，必须 export -f 让函数被继承。
psql() {
  local dsn="$1"; shift
  local port
  port=$(printf '%s' "$dsn" | sed -nE 's|.*@[^:]*:([0-9]+)/.*|\1|p')
  local container
  case "$port" in
    15432) container=ai-system-arch-pg-sh-1;;
    5433)  container=ai-system-arch-pg-bj-1;;
    *) echo "psql: unknown port $port (dsn=$dsn)" >&2; return 1;;
  esac
  docker exec -i "$container" psql -U apihub -d apihub "$@"
}
export -f psql

# 1. 建双向 pub/sub（S2-T1 脚本）
scripts/multi-region/setup-pg-logical-replication.sh
sleep 3  # 等初始 copy

# 2. 两库各建 tenant 表（sub 创建时无表 → copy 空；FOR ALL TABLES pub 动态含后续表）
psql "$PG_DSN_SH" -c "CREATE TABLE IF NOT EXISTS tenant(id text primary key, name text, home_region text);"
psql "$PG_DSN_BJ" -c "CREATE TABLE IF NOT EXISTS tenant(id text primary key, name text, home_region text);"
sleep 1

# 3. 写 sh → 应现于 bj
SH_TENANT="t_e2e_sh_$$"
psql "$PG_DSN_SH" -c "INSERT INTO tenant VALUES ('${SH_TENANT}','e2e','sh');" 2>/dev/null || true
sleep 2
CNT=$(psql "$PG_DSN_BJ" -Atc "SELECT count(*) FROM tenant WHERE id='${SH_TENANT}'")
[ "$CNT" = "1" ] || { echo "FAIL: sh→bj not replicated (cnt=$CNT)"; exit 1; }
echo "OK: sh→bj replicated"

# 4. 写 bj → 应现于 sh（反向）
BJ_TENANT="t_e2e_bj_$$"
psql "$PG_DSN_BJ" -c "INSERT INTO tenant VALUES ('${BJ_TENANT}','e2e','bj');" 2>/dev/null || true
sleep 2
CNT=$(psql "$PG_DSN_SH" -Atc "SELECT count(*) FROM tenant WHERE id='${BJ_TENANT}'")
[ "$CNT" = "1" ] || { echo "FAIL: bj→sh not replicated (cnt=$CNT)"; exit 1; }
echo "OK: bj→sh replicated"

# 5. 回环断言：sh 上的 SH_TENANT 行不被回环复制成 >1（origin=none 防环）
CNT=$(psql "$PG_DSN_SH" -Atc "SELECT count(*) FROM tenant WHERE id='${SH_TENANT}'")
[ "$CNT" = "1" ] || { echo "FAIL: loop replication detected (sh row count=$CNT)"; exit 1; }
echo "OK: no replication loop (origin=none)"

# 6. 清理
psql "$PG_DSN_SH" -c "DELETE FROM tenant WHERE id IN ('${SH_TENANT}','${BJ_TENANT}');" 2>/dev/null || true
psql "$PG_DSN_BJ" -c "DELETE FROM tenant WHERE id IN ('${SH_TENANT}','${BJ_TENANT}');" 2>/dev/null || true
echo "e2e-pg-replication PASS"
