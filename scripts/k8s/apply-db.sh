#!/usr/bin/env bash
# 幂等回放 scripts/init-db/*.sql 到运行中的 apihub-pg。
# 前提：init-db 脚本全幂等（CREATE...IF NOT EXISTS / ON CONFLICT DO NOTHING / DROP+CREATE POLICY）。
#
# 修正点（vs task-2-brief，R2b task-2 实测）：
#   1) 用 DB owner `apihub`（非 `apihub_app`）。`apihub_app` 无 schema public 的 CREATE
#      权限，首个 CREATE TABLE 即 `permission denied for schema public`。
#      `apihub` 是 PG superuser（见 CLAUDE.md「The Postgres superuser must be apihub」），
#      可跑 DDL；密码 `apihub_dev_pwd` 经 `-e PGPASSWORD=` 注入。
#   2) 不加 `--single-transaction`：11-notification-channels.sql 自带 BEGIN/COMMIT，
#      叠加会触发 `WARNING: there is already a transaction in progress`。
#      用 `psql -v ON_ERROR_STOP=1` 即可保证首错即停。
set -euo pipefail
cd "$(dirname "$0")/../.."

PG_USER="${PG_USER:-apihub}"
PG_PASSWORD="${PG_PASSWORD:-apihub_dev_pwd}"
PG_DB="${PG_DB:-apihub}"

echo "==> apply-db: replaying init-db scripts to apihub-pg (user=$PG_USER db=$PG_DB)"
cat scripts/init-db/*.sql \
  | docker exec -i -e PGPASSWORD="$PG_PASSWORD" apihub-pg \
      psql -U "$PG_USER" -d "$PG_DB" -v ON_ERROR_STOP=1

echo "==> tables now (public):"
docker exec -i -e PGPASSWORD="$PG_PASSWORD" apihub-pg \
  psql -U "$PG_USER" -d "$PG_DB" -tAc \
  "SELECT count(*) FROM pg_tables WHERE schemaname='public';"
