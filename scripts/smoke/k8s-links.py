#!/usr/bin/env python3.11
"""Stage 2 四链路 smoke：同步转发 / 异步任务 / 失败重试 / admin 聚合。

前置：Task 7 已起 kind 栈（12 pods Running）+ host compose 的 PG/Redis/Kafka/CH。

四条链路（直打服务，绕过 APISIX）：
  L1 sync   —— dispatcher 同步转发（/dispatch/{base_path}{path} → http backend）
  L2 async  —— 直投 Kafka task-requests → executor 消费 → task 表 succeeded
  L3 retry  —— 直投 Kafka task-failures → retry-svc 消费 → 跑完退避 → retry_task dead
  L4 admin  —— admin /v1/admin/dashboard 跨服务聚合（platform_admin only）

Step 0（契约摸清）结论已硬编码在下方常量里，详见各服务源码：
  - dispatcher 入口 ANY /dispatch/{rest:path}（services/dispatcher/.../routes.py）
  - resolver.resolve_by_path 拼 full_pattern = api.base_path + api_version.path
  - executor 消费 task-requests（services/executor/.../consumer.py::_handle）
      TaskMessage: task_id/api_id/api_version_id/backend_url/payload/timeout_seconds
      executor 先 mark_running(task 表 pending→running)，故直投前必须先在 PG 建好 pending task 行
  - retry 消费 task-failures（services/retry/.../consumer.py::_handle）
      FailureMessage: task_id/tenant_id/api_id/trace_id/backend_url/error_code/error_msg
      + max_attempts / backoff_base_ms；tenant_id 必须在 payload 或 header 里，否则早退
  - admin dashboard 需 is_platform_admin（auth.repository 读 tenant.metadata->>'is_platform_admin'）

注意：executor/retry/dispatcher 的 task 状态机落在 **task** 表（不是 task_instance）。
本脚本仅用 stdlib（urllib + subprocess），无需 venv。
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# 常量（与 seed 对齐 / 由 Step 0 摸清）
# ---------------------------------------------------------------------------
NAMESPACE = "apihub-system"
ADMIN_KEY = "ak_test_a_demo001"  # tenant_a / app_trading（见 02-seed.sql）
TENANT_ID = "tenant_a"
APP_ID = "app_trading"

# L1：新建一条指向 in-cluster mock-backend 的同步 API
#   mock-backend Service: mock-backend.apihub-system:80（GET {"ok":true}, POST {"ok":true,"echo":...}）
SMOKE_API_ID = "api_smoke_sync"
SMOKE_VER_ID = "ver_smoke_sync_v1"
SMOKE_BASE_PATH = "/smoke-sync"  # ApiCreate.base_path 须 ^/[a-z0-9-]+
SMOKE_API_PATH = "/echo"  # api_version.path
SMOKE_BACKEND_URL = "http://mock-backend.apihub-system:80"

TASK_TOPIC = "task-requests"
FAIL_TOPIC = "task-failures"

# auth 对 demo key 的 Redis 正缓存 key：ak:{sha256(plaintext)}（见 auth/apikey.py::cache_key）
DEMO_KEY_SHA256 = "c718af9f9532c71b958db43927477151b485bf23cb162a6f0a4920882c9b68f3"
AUTH_CACHE_KEY = f"ak:{DEMO_KEY_SHA256}"

# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def sh(cmd, check=True):
    """跑 shell 命令，返回 stdout（text）。失败抛异常带 stderr。"""
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


def write_json_file(path, obj):
    with open(path, "w") as f:
        f.write(json.dumps(obj))


def psql(sql_file_on_host):
    """把 host 上的 SQL 文件灌进 PG（apihub owner，绕过 RLS 便于 setup）。"""
    return sh(
        f"docker exec -i apihub-pg psql -U apihub -d apihub < {sql_file_on_host}",
    )


def redis_del(key):
    """清 Redis 某个 key（auth 正缓存用，确保 platform_admin 取最新）。

    compose 里 redis 要求密码（--requirepass apihub_dev_pwd）。apihub 默认用 db 0。
    """
    return sh(
        f"docker exec apihub-redis redis-cli -a apihub_dev_pwd -n 0 DEL {key}",
        check=False,
    )


def kafka_produce(topic, json_file_on_host):
    """单条 JSON 消息投到 topic（stdin 重定向 host 文件进容器）。"""
    return sh(
        f"docker exec -i apihub-kafka kafka-console-producer.sh "
        f"--bootstrap-server localhost:9094 --topic {topic} < {json_file_on_host}",
    )


# ---------------------------------------------------------------------------
# setup：建可用的同步 API + 放开 platform_admin + 清 auth 缓存
# ---------------------------------------------------------------------------


def setup():
    print("== setup ==")

    # 1) 给 tenant_a 打 platform_admin（L4 dashboard 需要超管）。
    #    auth.repository 读 tenant.metadata->>'is_platform_admin'（见 auth/repository.py）。
    sql = (
        "BEGIN;\n"
        f"UPDATE tenant SET metadata = COALESCE(metadata,'{{}}'::jsonb) "
        f"|| jsonb_build_object('is_platform_admin','true') WHERE id='{TENANT_ID}';\n"
        "COMMIT;\n"
    )
    with open("/tmp/smoke_setup.sql", "w") as f:
        f.write(sql)
    out = psql("/tmp/smoke_setup.sql")
    print("  platform_admin set:", out.strip().splitlines()[-1:] if out.strip() else "?")

    # 2) 清掉 auth 对 demo key 的正缓存（确保 platform_admin 取最新，非空缓存 5min）。
    #    Redis 当前为空（compose 刚起），这里保险起见仍清一次。
    redis_del(AUTH_CACHE_KEY)
    print("  auth cache flushed:", AUTH_CACHE_KEY)

    # 3) seed 一条指向 in-cluster mock-backend 的 published 同步 API（L1 用）。
    #    用 SQL upsert（确定性 + 幂等 + 固定 id），因为 api-registry REST 生成的
    #    api_id/version_id 是随机 uuid（无法指定），不便给 L2 的 task 行引用。
    #    seed 里三条 api 的 backend_url 都指向 example.internal（集群不可达），故另建。
    upsert = f"""
BEGIN;
INSERT INTO api (id, tenant_id, name, description, category, base_path,
                 tags, status, visibility, metadata, created_at, updated_at)
VALUES ('{SMOKE_API_ID}', '{TENANT_ID}', 'smoke-sync', 'smoke L1', 'smoke',
        '{SMOKE_BASE_PATH}', ARRAY['smoke'], 'published', 'tenant',
        '{{}}'::jsonb, NOW(), NOW())
ON CONFLICT (id) DO UPDATE SET status='published', updated_at=NOW();

INSERT INTO api_version (id, tenant_id, api_id, version, backend_type, backend_url,
                         method, path, ai_streaming, status, created_at, updated_at)
VALUES ('{SMOKE_VER_ID}', '{TENANT_ID}', '{SMOKE_API_ID}', 'v1', 'http',
        '{SMOKE_BACKEND_URL}', 'POST', '{SMOKE_API_PATH}', false,
        'published', NOW(), NOW())
ON CONFLICT (id) DO UPDATE SET backend_url=EXCLUDED.backend_url,
                               method=EXCLUDED.method, path=EXCLUDED.path,
                               status='published', updated_at=NOW();
COMMIT;
"""
    with open("/tmp/smoke_upsert.sql", "w") as f:
        f.write(upsert)
    out = psql("/tmp/smoke_upsert.sql")
    print("  smoke api upsert:", [l for l in out.strip().splitlines() if l.strip()][-2:])  # noqa: E741

    # 4) 确认 mock-backend 在集群内有 ClusterIP
    cip = sh(
        f"kubectl -n {NAMESPACE} get svc mock-backend " f"-o jsonpath='{{.spec.clusterIP}}'",
        check=False,
    ).strip()
    print("  mock-backend ClusterIP:", cip)


# ---------------------------------------------------------------------------
# L1：dispatcher 同步转发
# ---------------------------------------------------------------------------


def link1_sync():
    print("\n== L1 sync (dispatcher → mock-backend) ==")
    p = pf("dispatcher", 18001)
    try:
        path = f"/dispatch{SMOKE_BASE_PATH}{SMOKE_API_PATH}"  # /dispatch/smoke-sync/echo
        st, body = http(
            "POST",
            f"http://127.0.0.1:18001{path}",
            headers={"X-API-Key": ADMIN_KEY},
            data={"hello": "world"},
        )
        assert st == 200, f"L1 HTTP {st}: {body}"
        # mock-backend POST 返回 {"ok":true,"echo":...}
        assert isinstance(body, dict) and body.get("ok") is True, f"L1 unexpected body: {body}"
        print("  [L1 sync] OK", st, str(body)[:160])
    finally:
        p.terminate()


# ---------------------------------------------------------------------------
# L2：直投 task-requests → executor 消费 → task 表 succeeded
# ---------------------------------------------------------------------------


def link2_async():
    print("\n== L2 async (Kafka task-requests → executor → task succeeded) ==")
    task_id = f"task_smoke_{int(time.time())}"
    payload_str = json.dumps({"job": "smoke-l2", "n": 42})

    # executor 先 mark_running(task pending→running)，故必须先建 pending task 行
    insert_sql = (
        "BEGIN;\n"
        f"INSERT INTO task (id, tenant_id, api_id, api_version_id, app_id, status, "
        f"payload, request_id, created_at, updated_at) VALUES "
        f"('{task_id}', '{TENANT_ID}', '{SMOKE_API_ID}', '{SMOKE_VER_ID}', "
        f"'{APP_ID}', 'pending', $p${payload_str}$p$, 'req_smoke_l2', NOW(), NOW());\n"
        "COMMIT;\n"
    )
    with open("/tmp/smoke_task_insert.sql", "w") as f:
        f.write(insert_sql)
    psql("/tmp/smoke_task_insert.sql")
    print(f"  inserted pending task row: {task_id}")

    # 契约：services/executor/.../consumer.py::_handle / task_dispatcher.py 的 emit
    msg = {
        "task_id": task_id,
        "api_id": SMOKE_API_ID,
        "api_version_id": SMOKE_VER_ID,
        "backend_url": SMOKE_BACKEND_URL,
        "payload": payload_str,
        "timeout_seconds": 15.0,
    }
    write_json_file("/tmp/smoke_task_msg.json", msg)
    kafka_produce(TASK_TOPIC, "/tmp/smoke_task_msg.json")
    print(f"  produced to {TASK_TOPIC}: {task_id}")

    # 轮询 task 表直到 succeeded（executor mark_succeeded）
    ok = wait_for(
        lambda: sh(
            f"docker exec apihub-pg psql -U apihub -d apihub -t -A -c "
            f"\"select status from task where id='{task_id}';\""
        ).strip(),
        lambda s: s == "succeeded",
        timeout=20,
        interval=1.5,
        label=f"task {task_id} status",
    )
    assert ok, "L2 fail: task not succeeded (see executor logs)"
    final = sh(
        f"docker exec apihub-pg psql -U apihub -d apihub -t -A -c "
        f"\"select status, response_status, left(response_body,80) from task where id='{task_id}';\""
    ).strip()
    print("  [L2 async] OK", final)


# ---------------------------------------------------------------------------
# L3：直投 task-failures → retry-svc → 退避跑完 → retry_task dead
# ---------------------------------------------------------------------------


def link3_retry():
    print("\n== L3 retry (Kafka task-failures → retry-svc → retry_task dead) ==")
    fail_task_id = f"task_smoke_fail_{int(time.time())}"
    trace_id = f"trace_smoke_fail_{int(time.time())}"

    # 契约：services/retry/.../consumer.py::_handle + models.py::FailureMessage
    # backend_url 指向死地址 → executor 调用必失败 → 退避耗尽 → dead
    # tenant_id 必须在 payload（consumer 从 payload 或 header 取，缺失则早退）
    msg = {
        "task_id": fail_task_id,
        "tenant_id": TENANT_ID,
        "app_id": APP_ID,
        "api_id": SMOKE_API_ID,
        "trace_id": trace_id,
        "request_id": "req_smoke_l3",
        "api_version_id": SMOKE_VER_ID,
        "backend_url": "http://127.0.0.1:9/dead",  # executor pod 内无监听 → connect refused
        "payload": "{}",
        "error_code": "backend_unreachable",
        "error_msg": "smoke forced failure (dead backend)",
        "max_attempts": 2,
        "backoff_base_ms": 500,
    }
    write_json_file("/tmp/smoke_fail_msg.json", msg)
    kafka_produce(FAIL_TOPIC, "/tmp/smoke_fail_msg.json")
    print(f"  produced to {FAIL_TOPIC}: {fail_task_id} (trace {trace_id})")

    # 轮询 retry_task 直到出现 dead。
    # 注意：retry worker 调 executor :8003（见下 concern）—— 该端口在 K8s 里被 drop，
    # 每次 attempt 会耗满 30s connect timeout。max_attempts=2 ⇒ ~60s+，故 timeout 给 100s。
    ok = wait_for(
        lambda: sh(
            f"docker exec apihub-pg psql -U apihub -d apihub -t -A -c "
            f'"select status, retry_count, last_error_code from retry_task '
            f"where trace_id='{trace_id}' order by id desc limit 1;\""
        ).strip(),
        lambda s: s.startswith("dead"),
        timeout=100,
        interval=3,
        label=f"retry_task(trace={trace_id})",
    )

    rows = sh(
        f"docker exec apihub-pg psql -U apihub -d apihub -c "
        f'"select status, count(*), max(last_error_code) from retry_task '
        f"where trace_id='{trace_id}' group by status;\""
    ).strip()
    print("  retry_task rows:", rows.replace("\n", " | "))
    assert ok, f"L3 fail: retry_task never reached 'dead' ({rows!r})"
    print("  [L3 retry] OK dead reached")


# ---------------------------------------------------------------------------
# L4：admin dashboard 跨服务聚合（platform_admin）
# ---------------------------------------------------------------------------


def link4_admin():
    print("\n== L4 admin (dashboard aggregation) ==")
    p = pf("admin", 18006)
    try:
        st, body = http(
            "GET", "http://127.0.0.1:18006/v1/admin/dashboard", headers={"X-API-Key": ADMIN_KEY}
        )
        assert st == 200, f"L4 HTTP {st}: {body}"
        # DashboardResponse 至少带 tenants 聚合
        assert isinstance(body, dict) and "tenants" in body, f"L4 unexpected body: {body}"
        print("  [L4 admin] OK", st, str(body)[:200])
    finally:
        p.terminate()


# ---------------------------------------------------------------------------
# 轮询助手
# ---------------------------------------------------------------------------


def wait_for(probe, predicate, *, timeout, interval, label=""):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = probe()
        except Exception as e:  # noqa: BLE001
            last = f"<probe error: {e}>"
        if predicate(last or ""):
            return True
        time.sleep(interval)
    print(f"    [wait] TIMEOUT {label}: last={last!r}")
    return False


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if os.environ.get("SKIP_SETUP") != "1":
        setup()

    links = [link1_sync, link2_async, link3_retry, link4_admin]
    fails = []
    for fn in links:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            fails.append((fn.__name__, str(e)))
            print(f"  FAIL {fn.__name__}: {e}")

    print("\n" + "=" * 60)
    if fails:
        for n, e in fails:
            print(f"  - {n}: {e}")
        print(f"\n{len(links) - len(fails)}/{len(links)} LINKS GREEN")
        sys.exit(1)
    print("ALL 4 LINKS GREEN")
