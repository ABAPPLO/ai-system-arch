#!/bin/bash
# 校验 tenant-affinity.lua 可被 lua 解析（无 lua 解释器则跳过 + 提示）。
set -euo pipefail
LUA="${LUA:-$(command -v lua || command -v luajit || true)}"
FILE="deploy/apisix/plugins/tenant-affinity.lua"
if [ -z "$LUA" ]; then
  echo "WARN: no lua/luajit; skipping parse check for $FILE" >&2; exit 0
fi
"$LUA" -e "assert(loadfile('$FILE'))" || { echo "FAIL: $FILE syntax error"; exit 1; }
echo "OK: $FILE parses"
