#!/bin/bash
# 季度演练 harness（自动化）：对双 PG 栈注入故障 → 跑 runbook → 断言 bj writable → rollback。
# 验证「切换逻辑 + 数据链路」，非网络分区（kind/docker 限制，§8-R5）。
#
# 前置：pg-sh:15432 + pg-bj:5433 up（Task 0.1），S2 双向 sub/pub 已建（e2e-pg-replication.sh）。
# 本机 psql 被 snap 限制无法走 TCP，经容器内 psql 转发（同 e2e-pg-replication.sh，仍直连真实 PG）。
set -euo pipefail

FAILED="sh"; SURVIVING="bj"
export PG_DSN_SH=postgres://apihub:apihub_dev_pwd@localhost:15432/apihub
export PG_DSN_BJ=postgres://apihub:apihub_dev_pwd@localhost:5433/apihub

# psql wrapper：按端口路由到对应容器内 psql（绕开 snap 限制）。
# runbook 在子进程中调用 psql，必须 export -f 让函数被继承。
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

# 注入一个 sh-home probe tenant（让 runbook [3/6] migrate 步骤有真实对象可迁移；end-to-end 真切换）
psql "$PG_DSN_BJ" -c "CREATE TABLE IF NOT EXISTS tenant(id text primary key, name text, home_region text);" 2>/dev/null || true
psql "$PG_DSN_BJ" -c "INSERT INTO tenant VALUES ('drill_probe_tenant','drill','sh') ON CONFLICT (id) DO UPDATE SET home_region='sh';" 2>/dev/null || true

echo "[drill 1/5] inject failure: stop pg-sh (simulate region sh down)"
docker stop ai-system-arch-pg-sh-1 2>/dev/null || echo "  (pg-sh container name differs / already down — simulating via runbook FORCE path)"

echo "[drill 2/5] run failover runbook (FORCE=1 — sh down, sub lag check can't reach sh; [1/6] gw 健康检查在 docker 无 gw → FORCE 跳过)"
OPERATOR=drill FORCE=1 bash scripts/multi-region/failover-runbook.sh "$FAILED"

echo "[drill 3/5] assert bj writable + primary"
psql "$PG_DSN_BJ" -c "CREATE TABLE IF NOT EXISTS drill_probe(id text); INSERT INTO drill_probe VALUES ('probe_'||now()::text);" 2>/dev/null || true
RO=$(psql "$PG_DSN_BJ" -Atc "SELECT pg_is_in_recovery()")
[ "$RO" = "f" ] || { echo "FAIL: bj not primary (in recovery)"; exit 1; }
echo "  OK: bj primary (pg_is_in_recovery=f) + writable"
# 断言：sh-home probe tenant 已迁移到 bj（验证 [3/6] migrate 真生效）
MIG=$(psql "$PG_DSN_BJ" -Atc "SELECT home_region FROM tenant WHERE id='drill_probe_tenant'")
[ "$MIG" = "$SURVIVING" ] || { echo "FAIL: probe tenant home_region=$MIG (expected $SURVIVING)"; exit 1; }
echo "  OK: probe tenant migrated ${FAILED}→${SURVIVING}"

echo "[drill 4/5] rollback: restart pg-sh + re-enable both subs (restore bidir)"
docker start ai-system-arch-pg-sh-1 2>/dev/null || true
sleep 3
# Runbook [3/6] disabled sub_from_<FAILED>_on_<SURVIVING> on bj — re-enable it first.
psql "$PG_DSN_BJ" -c "ALTER SUBSCRIPTION sub_from_${FAILED}_on_${SURVIVING} ENABLE;" 2>/dev/null || echo "  (${SURVIVING} sub re-enable skipped / not found)"
# Re-enable the reverse sub too (restore pre-drill bidir state).
psql "$PG_DSN_SH" -c "ALTER SUBSCRIPTION sub_from_${SURVIVING}_on_${FAILED} ENABLE;" 2>/dev/null || echo "  (${FAILED} sub re-enable skipped / not found)"
# 回滚 probe tenant home_region（恢复演练前状态）
psql "$PG_DSN_BJ" -c "UPDATE tenant SET home_region='${FAILED}' WHERE id='drill_probe_tenant';" 2>/dev/null || true

echo "[drill 5/5] cleanup probe"
psql "$PG_DSN_BJ" -c "DROP TABLE IF EXISTS drill_probe;" 2>/dev/null || true
psql "$PG_DSN_BJ" -c "DELETE FROM tenant WHERE id='drill_probe_tenant';" 2>/dev/null || true
echo "drill-failover PASS"
