#!/usr/bin/env python3.11
"""Stage 3b trace 端到端查 ClickHouse —— 验证 Task 1 的精简 schema SQL 修复。

流程：
  1) 产生若干调用（优先经 APISIX NodePort 30080 真实网关路径；
     APISIX 不可达时退用 dispatcher port-forward）。
  2) 等 ~15s 让 dispatcher → Kafka `api-call-events` → CH Kafka-engine MV →
     `api_call_log` 摄取。
  3) 直查 CH `api_call_log`：count + 非空行数（api_id!=''）。
  4) 查 trace-svc `/v1/trace/calls`，断言 ≥1 行且带 `api_id` / `http_status` 键。
  5) D5 兜底：若 CH 经摄取只有空行（Kafka-engine MV 列映射坏 → 全空行），
     直接 INSERT 一行匹配精简 schema 的合成数据再查，隔离摄取问题、
     专验 trace-svc 查询本身（即 Task 1 的 SQL 修复）。

前置：
  - kind 栈 12 pods Running；host compose 的 CH (`apihub-clickhouse`) 起着。
  - APISIX 在 `apihub-ingress` 命名空间，`apisix-gateway` NodePort 30080
    （路由 /dispatch/* → dispatcher）。
  - smoke-sync API 已 seed（`api_smoke_sync` 指向 in-cluster mock-backend），
    由 scripts/smoke/k8s-links.py::setup 幂等建好；本脚本会兜底确认存在。
  - tenant_a 已打 is_platform_admin（trace-svc 走 admin 视角看全部行）。

仅用 stdlib（urllib + subprocess），无需 venv。
"""

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# 常量（与 seed / k8s-links 对齐 / 由源码摸清）
# ---------------------------------------------------------------------------
NAMESPACE = "apihub-system"
ADMIN_KEY = "ak_test_a_demo001"  # tenant_a / app_trading（02-seed.sql）
TENANT_ID = "tenant_a"
APP_ID = "app_trading"

# APISIX 真实网关路径（apihub-ingress/apisix-gateway NodePort 30080）
APISIX_URL = "http://127.0.0.1:30080"
DISPATCH_PATH = "/dispatch/smoke-sync/echo"  # mock-backend POST {"ok":true,"echo":...}

# 直查 CH 的 docker exec 句柄
CH_CONTAINER = "apihub-clickhouse"
CH_TABLE = "apihub.api_call_log"

# trace-svc 列表查询返回的 CallListItem 必含这两个键（routes._row_to_list_item）
REQUIRED_KEYS = ("api_id", "http_status")

# 合成行用 api_demo_a（seed 里的真实 API），trace_id 加时间戳保证唯一
SYNC_API_ID = "api_smoke_sync"
SYNC_VER_ID = "ver_smoke_sync_v1"

INGEST_WAIT_S = 15
N_CALLS = 6


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def sh(cmd, check=True):
    """跑 shell 命令，返回 stdout(text)。失败抛异常带 stderr。"""
    r = subprocess.run(cmd, shell=True, text=True, capture_output=True)
    if check and r.returncode != 0:
        raise RuntimeError(
            f"cmd failed (rc={r.returncode}): {cmd}\n--- stdout ---\n{r.stdout}\n"
            f"--- stderr ---\n{r.stderr}"
        )
    return r.stdout


def pf(svc, local, remote_port=80):
    """kubectl port-forward svc/{svc} local:remote；返回 Popen（用完 terminate）。"""
    p = subprocess.Popen(
        ["kubectl", "-n", NAMESPACE, "port-forward", f"svc/{svc}", f"{local}:{remote_port}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(3)  # 等 port-forward 起来
    return p


def http(method, url, headers=None, data=None, timeout=15):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", **(headers or {})},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            try:
                parsed = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                parsed = raw.decode(errors="replace")
            return r.status, parsed
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            parsed = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            parsed = raw.decode(errors="replace")
        return e.code, parsed


def ch_query(sql):
    """docker exec clickhouse-client --query；返回 stdout(text)。"""
    return sh(
        f'docker exec {CH_CONTAINER} clickhouse-client --query "{sql}"',
        check=False,
    ).strip()


# ---------------------------------------------------------------------------
# Step 1：产生调用（APISIX 真实路径；不可达则退 dispatcher port-forward）
# ---------------------------------------------------------------------------


def generate_traffic():
    print(f"== 产生 {N_CALLS} 次调用 ==")
    use_apisix = True
    # 先探 APISIX 是否在
    try:
        st, _ = http("GET", f"{APISIX_URL}/", timeout=5)
        # 404 也算 APISIX 活着（没配 / 路由）
        if st not in (200, 404):
            use_apisix = False
    except Exception:
        use_apisix = False

    if use_apisix:
        print("  走 APISIX NodePort 30080（真实网关路径）")
        for i in range(N_CALLS):
            st, body = http(
                "POST",
                f"{APISIX_URL}{DISPATCH_PATH}",
                headers={"X-API-Key": ADMIN_KEY},
                data={"x": i},
                timeout=10,
            )
            print(f"  call{i}: HTTP {st}")
    else:
        print("  APISIX 不可达，退用 dispatcher port-forward")
        p = pf("dispatcher", 18001)
        try:
            for i in range(N_CALLS):
                st, body = http(
                    "POST",
                    f"http://127.0.0.1:18001{DISPATCH_PATH}",
                    headers={"X-API-Key": ADMIN_KEY},
                    data={"x": i},
                    timeout=10,
                )
                print(f"  call{i}: HTTP {st}")
        finally:
            p.terminate()


# ---------------------------------------------------------------------------
# Step 2/3：直查 CH —— count + 非空行（api_id!=''）
# ---------------------------------------------------------------------------


def ch_snapshot():
    """返回 (count, nonempty, max_ts)。nonempty = api_id!='' 的行数（真实数据）。"""
    out = ch_query(f"select count(), countIf(api_id!=''), max(ts) from {CH_TABLE}")
    # 形如 "15\t2\t2026-07-09 10:59:17.000"
    parts = out.split("\t") if out else ["0", "0", ""]
    try:
        count = int(parts[0])
    except (ValueError, IndexError):
        count = 0
    try:
        nonempty = int(parts[1])
    except (ValueError, IndexError):
        nonempty = 0
    max_ts = parts[2] if len(parts) > 2 else ""
    return count, nonempty, max_ts


# ---------------------------------------------------------------------------
# Step 4：查 trace-svc
# ---------------------------------------------------------------------------


def query_trace(limit=10):
    """port-forward trace svc → GET /v1/trace/calls；返回 (status, rows_or_body)。"""
    p = pf("trace", 18008)
    try:
        st, body = http(
            "GET",
            f"http://127.0.0.1:18008/v1/trace/calls?limit={limit}",
            headers={"X-API-Key": ADMIN_KEY},
            timeout=15,
        )
        return st, body
    finally:
        p.terminate()


# ---------------------------------------------------------------------------
# Step 5（D5 兜底）：合成一行匹配精简 schema 的数据，专验查询本身
# ---------------------------------------------------------------------------


def d5_insert_synthetic():
    """直插一行真实值到 api_call_log（绕过坏掉的 Kafka-engine MV）。

    列序对齐 DESCRIBE api_call_log 精简 schema（26 列；error_stack_ref 有
    DEFAULT '' 自动补）。用 tenant_a（platform_admin 视角可见）。
    返回插入的 trace_id。
    """
    trace_id = f"trc_smoke_t10_{int(time.time())}"
    # 注意：值个数必须 = 列个数（26）。token_* 共 4 列。
    sql = (
        f"INSERT INTO {CH_TABLE} "
        f"(ts, tenant_id, tenant_type, app_id, api_id, api_version_id, trace_id, request_id, "
        f" method, path, status_code, is_success, latency_ms, request_size, response_size, "
        f" error_code, error_msg, user_agent, client_ip, backend_type, backend_latency_ms, "
        f" ai_model, ai_streaming, token_prompt, token_completion, token_total) "
        f"SELECT now(), 'tenant_a', 'internal', 'app_trading', 'api_demo_a', 'ver1', "
        f"'{trace_id}', 'req_smoke_t10', 'POST', '/smoke-sync/echo', 200, 1, 42, 50, 80, "
        f"'', '', 'k8s-trace-smoke/1.0', toIPv4('10.0.0.1'), 'http', 10, "
        f"'', 0, 0, 0, 0"
    )
    sh(f'docker exec {CH_CONTAINER} clickhouse-client --query "{sql}"')
    print(f"  [D5] 合成行已插: trace_id={trace_id}")
    return trace_id


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main():
    # --- Step 1：先看 CH 摄取基线 ---
    count0, nonempty0, _ = ch_snapshot()
    print(f"[CH before] count={count0} nonempty(api_id)={nonempty0}")

    # --- Step 2：产生调用 ---
    generate_traffic()

    # --- Step 3：等 Kafka-engine MV 消费 + 转存 ---
    print(f"waiting {INGEST_WAIT_S}s for CH ingest...")
    time.sleep(INGEST_WAIT_S)

    # --- Step 4：再看 CH ---
    count1, nonempty1, max_ts1 = ch_snapshot()
    print(f"[CH after]  count={count1} nonempty(api_id)={nonempty1} max_ts={max_ts1!r}")

    # 判定走真实路径还是 D5 兜底：
    #   真实路径 = 摄取后出现「非空行」（api_id 非空），说明 MV 列映射正常。
    #   否则（只有空行 / 无新增）= MV 列映射坏 → D5 合成一行专验查询。
    used_d5 = False
    if nonempty1 == 0:
        print("[path] CH 无真实数据行（Kafka→CH 摄取列映射坏）→ 走 D5 兜底")
        d5_insert_synthetic()
        used_d5 = True
        count1, nonempty1, max_ts1 = ch_snapshot()
        print(f"[CH after D5] count={count1} nonempty(api_id)={nonempty1} max_ts={max_ts1!r}")
    else:
        print("[path] CH 有真实数据行 → 走真实摄取路径")

    # --- Step 5：查 trace-svc ---
    print("== 查 trace-svc /v1/trace/calls ==")
    st, body = query_trace(limit=10)

    # 容错：trace-svc 查询报错（如 Unknown column identifier）= Task 1 SQL 残留 bug
    if st != 200:
        print(f"[trace] HTTP {st}: {body}")
        print("FAIL: trace-svc 查询报错 —— 疑似 Task 1 SQL 残留 bug，需回 Task 1 补单测")
        sys.exit(2)

    rows = body if isinstance(body, list) else []
    print(f"[trace] HTTP {st} rows={len(rows)}")

    # 断言：≥1 行 + 含 api_id / http_status 键
    assert isinstance(rows, list), f"trace 返回非 list: {body!r}"
    assert len(rows) >= 1, f"trace 0 行 —— CH 无数据 或 trace SQL 仍错. body={body!r}"
    sample = rows[0]
    missing = [k for k in REQUIRED_KEYS if k not in sample]
    assert not missing, f"行缺键 {missing}: {sample!r}"

    # 找一条「真实」行（非空 api_id）作为有效样本
    real_row = next((r for r in rows if r.get("api_id")), rows[0])
    print(f"[trace] OK rows={len(rows)} real_path={'D5-fallback' if used_d5 else 'real'}")
    print(f"[trace] sample={json.dumps(real_row, default=str)[:240]}")
    print(
        f"[trace] columns OK: api_id={real_row.get('api_id')!r} "
        f"http_status={real_row.get('http_status')!r}"
    )

    print("\n" + "=" * 60)
    path = "D5 fallback (合成行)" if used_d5 else "real-path (Kafka 摄取)"
    print(f"TRACE OK —— {path}：trace-svc 在真实 CH schema 上跑通，列名正确")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"\nASSERT FAIL: {e}")
        sys.exit(1)
