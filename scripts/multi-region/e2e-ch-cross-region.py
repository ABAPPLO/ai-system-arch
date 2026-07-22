#!/usr/bin/env python3
"""S4-T4 e2e：peer_client 真连对端 CH + query_union_peer 真行拼接（live，非 mock）。

覆盖单元测试（mock）打不到的 gap：
  1. peer_client 真实 TCP 连接到对端 Region 的 ClickHouse（ch-bj）。
  2. query_union_peer 把 local(ch-sh) + peer(ch-bj) 的真行拼到同一 list。

拓扑（见 e2e-ch-cross-region.sh）：
  ch-sh 127.0.0.2:18123  ← 本地 Region（sh）  →  e2e_t.x = 1
  ch-bj 127.0.0.3:18123  ← 对端 Region（bj）  →  e2e_t.x = 2

关键约束：apihub_core.clickhouse 的 peer_client 复用 settings.ch_port
（strip 掉 peer_region_ch_host 的 :port），故 local 与 peer 必须同端口；
两 Region 用不同 loopback IP（127.0.0.2 / 127.0.0.3）区分。

运行：repo 根 `.venv/bin/python scripts/multi-region/e2e-ch-cross-region.py`
前置：先跑 e2e-ch-cross-region.sh 建表 + 写行 + 可达性校验。
"""

from __future__ import annotations

import os
import sys

# --- env 必须在 import apihub_core 之前设进 os.environ（pydantic-settings 实例化时读 env）。
# Settings 必填项（pg_*/redis_* 无默认值）给 dummy 值——init_clickhouse 只碰 CH，不碰 PG/Redis。
_BASE_ENV = {
    "ENV": "dev",  # 避免 prod secure-secret 校验
    "PG_HOST": "dummy",
    "PG_USER": "dummy",
    "PG_PASSWORD": "dummy",
    "REDIS_HOST": "dummy",
    "HOME_REGION": "sh",
    # 本地 Region CH（ch-sh）
    "CH_HOST": "127.0.0.2",
    "CH_PORT": "18123",
    "CH_USERNAME": "default",
    "CH_PASSWORD": "apihub_dev_pwd",
    "CH_DATABASE": "apihub",
}
_PEER_ENV = {
    # 对端 Region CH（ch-bj）——host 里的 :port 会被 clickhouse.py strip 掉，
    # 实际用 CH_PORT(18123)。所以这里写 http://127.0.0.3:18123 只是标注 host。
    "PEER_REGION_CH_HOST": "http://127.0.0.3:18123",
    "PEER_REGION_CH_USER": "default",
    "PEER_REGION_CH_PASSWORD": "apihub_dev_pwd",
}


def _apply_env(extra: dict[str, str] | None) -> None:
    os.environ.update(_BASE_ENV)
    if extra:
        os.environ.update(extra)
    else:
        for k in _PEER_ENV:
            os.environ.pop(k, None)
    # 绕过宿主机 socks5/http 代理（会劫持 127.x 请求返回 502）
    for k in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        os.environ[k] = ""


def _fresh_settings_and_init():
    from apihub_core import clickhouse
    from apihub_core.config import get_settings

    get_settings.cache_clear()
    s = get_settings()
    clickhouse.close_clickhouse()
    clickhouse.init_clickhouse(s)
    return s


def main() -> int:
    failures: list[str] = []

    # ---- 场景 A：peer 已配 → query_union_peer 返回 BOTH regions 的行（live peer_client 连接 + 真行拼接）
    print("=== 场景 A：peer_region_ch_host 已配 → 期望 [1, 2]（双区拼接）===")
    _apply_env(_PEER_ENV)
    s = _fresh_settings_and_init()
    from apihub_core import clickhouse as ch

    assert ch._peer_client is not None, "peer_client 未创建（PEER_REGION_CH_HOST 未生效？）"
    print(f"  local CH : {s.ch_host}:{s.ch_port}")
    print(
        f"  peer  CH : {s.peer_region_ch_host}  peer_client alive = {ch._peer_client is not None}"
    )
    rows = ch.query_union_peer(
        "SELECT x FROM e2e_t ORDER BY x",
        "SELECT x FROM e2e_t ORDER BY x",
        None,
        force_tenant_id=None,  # peer_sql 要求 admin scope（force_tenant_id=None）
    )
    xs = sorted(r["x"] for r in rows)
    print(f"  query_union_peer rows.x = {xs}")
    if xs == [1, 2]:
        print("  PASS: 双区行均出现（1 来自 ch-sh + 2 来自 ch-bj）→ peer_client 真连 + 真行拼接 OK")
    else:
        failures.append(f"场景 A 期望 [1, 2]，实际 {xs}（peer_client 未连到 ch-bj 或拼接失败）")

    # ---- 场景 B：peer 未配 → 单 Region degrade，仅 local 行（无异常）
    print("\n=== 场景 B：peer_region_ch_host 未配 → 期望仅 [1]（单区降级）===")
    _apply_env(None)
    s = _fresh_settings_and_init()
    assert ch._peer_client is None, "peer_client 应为 None（未配 peer）"
    rows = ch.query_union_peer(
        "SELECT x FROM e2e_t ORDER BY x",
        "SELECT x FROM e2e_t ORDER BY x",  # peer_sql 非空但 peer_client=None → degrade-to-local
        None,
        force_tenant_id=None,
    )
    xs = sorted(r["x"] for r in rows)
    print(f"  query_union_peer rows.x = {xs}  (peer_client = {ch._peer_client})")
    if xs == [1]:
        print("  PASS: 未配 peer 时仅返回 local 行（1）→ 单区降级 OK，无异常")
    else:
        failures.append(f"场景 B 期望 [1]，实际 {xs}")

    ch.close_clickhouse()
    print("\n" + "=" * 60)
    if failures:
        print("FAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(
        "ALL PASS: peer_client live 连接 + query_union_peer 真行拼接（双区）/ 单区降级 均验证通过"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
