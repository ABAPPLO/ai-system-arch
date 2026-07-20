#!/usr/bin/env bash
# S4-T4 e2e：双 ClickHouse 跨区数据准备 + 可达性校验（data-setup / reachability）。
#
# 真正的 peer_client live 连接 + query_union_peer 行拼接证明见伴生
# e2e-ch-cross-region.py（mock 覆盖不到的 gap：peer_client 真连对端 CH + 真行拼接）。
#
# 拓扑（docker-compose.multi-region.yml）：
#   ch-sh  127.0.0.2:18123  ← 本地 Region（sh）
#   ch-bj  127.0.0.3:18123  ← 对端 Region（bj）
# 两 CH 同主机端口（18123）+ 不同 loopback IP：apihub_core.clickhouse 的 peer_client
# 复用 settings.ch_port（strip 掉 peer_region_ch_host 的 :port），故 local/peer 必须同端口。
# 选 18123 避开 dev 栈 apihub-clickhouse（0.0.0.0:8123）。
set -euo pipefail

CH_SH_HOST=127.0.0.2
CH_BJ_HOST=127.0.0.3
CH_PORT=18123
CH_USER=default
CH_PASSWORD=apihub_dev_pwd

# 绕过宿主机 HTTP 代理（本机配了 socks5/http 代理，会劫持 127.x 请求返回 502）。
export http_proxy= https_proxy= all_proxy= HTTP_PROXY= HTTPS_PROXY= ALL_PROXY=
CURL=(curl --noproxy '*' -s --max-time 10 -u "${CH_USER}:${CH_PASSWORD}")

# CLICKHOUSE_DB=apihub 只建库不设为用户默认库；默认 currentDatabase()='default'。
# 显式 ?database=apihub 让表落在 apihub 库——与 Python harness 的 CH_DATABASE=apihub
# 及线上部署约束（CH_DATABASE=apihub）对齐。
DB=apihub
q() { # q <host> <sql>           GET 查询（readonly）
  "${CURL[@]}" "http://$1:${CH_PORT}/?database=${DB}&query=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$2")"
}
post() { # post <host> <sql>     POST 写入（CREATE/INSERT 需 POST + body）
  "${CURL[@]}" -X POST "http://$1:${CH_PORT}/?database=${DB}" --data-binary "$2"
}

echo "=== 1) 可达性：每 CH 单独 SELECT 1 ==="
for h in "$CH_SH_HOST" "$CH_BJ_HOST"; do
  r=$(q "$h" 'SELECT 1')
  [ "$r" = "1" ] && echo "  $h:${CH_PORT} OK (SELECT 1 -> 1)" || { echo "  $h:${CH_PORT} FAIL -> '$r'"; exit 1; }
done

echo "=== 2) 建表 + 各写一行（ch-sh=1, ch-bj=2）==="
for entry in "$CH_SH_HOST 1" "$CH_BJ_HOST 2"; do
  read -r h v <<< "$entry"
  post "$h" 'CREATE TABLE IF NOT EXISTS e2e_t (x UInt8) ENGINE=Memory' >/dev/null
  post "$h" "INSERT INTO e2e_t VALUES ($v)" >/dev/null
  rows=$(q "$h" 'SELECT x FROM e2e_t ORDER BY x')
  echo "  $h:${CH_PORT} e2e_t rows: ${rows//$'\n'/ }"
done

echo "=== 3) 基线计数：单 CH 各自只看到本区那行 ==="
sh_rows=$(q "$CH_SH_HOST" 'SELECT count() FROM e2e_t')
bj_rows=$(q "$CH_BJ_HOST" 'SELECT count() FROM e2e_t')
echo "  ch-sh rows=${sh_rows}  ch-bj rows=${bj_rows}"
[ "$sh_rows" = "1" ] && [ "$bj_rows" = "1" ] || { echo "  FAIL: 期望各 1 行（数据未隔离 / 写入失败）"; exit 1; }

sh_only=$(q "$CH_SH_HOST" 'SELECT x FROM e2e_t')
bj_only=$(q "$CH_BJ_HOST" 'SELECT x FROM e2e_t')
echo "  ch-sh.x=${sh_only}  ch-bj.x=${bj_only}"
[ "$sh_only" = "1" ] && [ "$bj_only" = "2" ] || { echo "  FAIL: 期望 ch-sh=1 / ch-bj=2"; exit 1; }

echo "=== 4) 真值由 .py 跨区拼接证明（peer_client live + query_union_peer）==="
echo "  data-setup + reachability PASS — 现在跑 e2e-ch-cross-region.py"
