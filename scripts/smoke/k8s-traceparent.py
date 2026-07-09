#!/usr/bin/env python3
"""traceparent 贯通回归 smoke（kind）。

验证 dispatcher → Kafka → executor 在 Jaeger 上是同一条连通 trace：
  1) seed 一条 tenant_a 的 async API（指向 in-cluster mock-backend）
  2) 经 APISIX POST /dispatch/<async>/work → dispatcher → Kafka task-requests → executor
  3) 等 BatchSpanProcessor 导出（~10s）
  4) 查 Jaeger /api/traces?service=dispatcher，断言存在一条 trace 同时含
     dispatcher 的 SERVER span 与 executor 的 'kafka.consume task-requests' span

退出码：0 OK / 1 assert fail / 2 env unavailable。
"""

import json
import sys
import time
import urllib.request

NAMESPACE = "apihub-system"
APISIX_URL = "http://127.0.0.1:30080"
JAEGER_URL = "http://127.0.0.1:16686"
DEMO_KEY = "ak_test_a_demo001"
ASYNC_BASE_PATH = "/smoke-async"
ASYNC_API_ID = "smoke-async-api"
ASYNC_VER_ID = "smoke-async-v1"
TENANT_ID = "tenant_a"
EXPORT_WAIT_S = 12
POLL_ATTEMPTS = 6
POLL_INTERVAL_S = 5


def http(method, url, headers=None, data=None, timeout=15):
    req = urllib.request.Request(url, method=method, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def sh(cmd):
    import subprocess

    return subprocess.run(cmd, shell=True, capture_output=True, text=True, check=True).stdout


def seed_async_api():
    """给 tenant_a seed 一条 async_task API（指向 mock-backend）。

    用 superuser `apihub`（与 scripts/smoke/k8s-links.py::psql 一致）绕过 RLS，
    便于 setup；apihub_app 会触发 RLS WITH CHECK 失败。
    """
    sql = f"""
    INSERT INTO api (id, tenant_id, name, description, category, base_path, tags, status, visibility)
    VALUES ('{ASYNC_API_ID}', '{TENANT_ID}', 'smoke async', 'trace smoke', 'smoke',
            '{ASYNC_BASE_PATH}', ARRAY['smoke'], 'published', 'tenant')
    ON CONFLICT (id) DO NOTHING;
    INSERT INTO api_version (id, tenant_id, api_id, version, backend_type, backend_url, method, path, status)
    VALUES ('{ASYNC_VER_ID}', '{TENANT_ID}', '{ASYNC_API_ID}', '1.0', 'async_task',
            'http://mock-backend.{NAMESPACE}/echo', 'POST', '/work', 'published')
    ON CONFLICT (id) DO NOTHING;
    """
    with open("/tmp/_tp_seed.sql", "w") as f:
        f.write(sql)
    sh("docker exec -i apihub-pg psql -U apihub -d apihub -v ON_ERROR_STOP=1 < /tmp/_tp_seed.sql")


def trigger_async():
    """经 APISIX 触发异步任务，返回 HTTP 状态。"""
    url = f"{APISIX_URL}/dispatch{ASYNC_BASE_PATH}/work"
    st, body = http(
        "POST",
        url,
        headers={"X-API-Key": DEMO_KEY, "Content-Type": "application/json"},
        data=b'{"hello":"trace"}',
    )
    print(f"  trigger POST {url} -> HTTP {st} {body[:120]!r}")
    return st


def find_connected_trace():
    """查 Jaeger，返回是否有一条 trace 同时含 dispatcher SERVER span 与 executor consume span。"""
    url = f"{JAEGER_URL}/api/traces?service=dispatcher&limit=40&lookback=1h"
    st, body = http("GET", url, timeout=20)
    if st != 200:
        print(f"  Jaeger HTTP {st}: {body[:200]!r}")
        return False, 0
    data = json.loads(body)
    traces = data.get("data", [])
    for tr in traces:
        procs = {pid: p.get("serviceName") for pid, p in tr.get("processes", {}).items()}
        spans = tr.get("spans", [])
        has_dispatcher = any(procs.get(s.get("processID")) == "dispatcher" for s in spans)
        has_consume = any(s.get("operationName") == "kafka.consume task-requests" for s in spans)
        if has_dispatcher and has_consume:
            return True, len(traces)
    return False, len(traces)


def main():
    print("== seed tenant_a async API ==")
    seed_async_api()

    print("== trigger async task via APISIX → dispatcher → Kafka → executor ==")
    st = trigger_async()
    if st not in (200, 202):
        print(f"FAIL: trigger HTTP {st} (APISIX/dispatcher 不通？)")
        sys.exit(2)

    # 先等 BatchSpanProcessor 默认 5s flush，再轮询：OTLP→collector→Jaeger 摄取有抖动，
    # 一次性查询易在边界 miss（实测首条 consume span 落库可达 ~15-30s）。
    print(f"== wait {EXPORT_WAIT_S}s then poll Jaeger (BatchSpanProcessor export + ingest) ==")
    time.sleep(EXPORT_WAIT_S)

    print("== query Jaeger for connected trace ==")
    ok, n = False, 0
    for attempt in range(1, POLL_ATTEMPTS + 1):
        ok, n = find_connected_trace()
        if ok:
            print(
                f"TRACEPARENT OK —— 找到连通 trace（dispatcher SERVER + executor kafka.consume），"
                f"共扫 {n} 条（第 {attempt}/{POLL_ATTEMPTS} 次轮询）"
            )
            sys.exit(0)
        print(
            f"  attempt {attempt}/{POLL_ATTEMPTS}: 未命中（扫 {n} 条），{POLL_INTERVAL_S}s 后重试…"
        )
        time.sleep(POLL_INTERVAL_S)

    print(
        f"FAIL: 未找到连通 trace（扫了 {n} 条，{POLL_ATTEMPTS} 次轮询）—— "
        f"executor consume span 未链接到 dispatcher trace"
    )
    sys.exit(1)


if __name__ == "__main__":
    main()
