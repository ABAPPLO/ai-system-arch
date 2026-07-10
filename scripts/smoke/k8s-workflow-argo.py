#!/usr/bin/env python3
"""真 Argo CRD e2e smoke（kind）。

经 APISIX → dispatcher /v1/jobs → workflow-svc（真 Argo）：
  A) succeeded 主路径：2-step busybox echo → 轮询到 succeeded，
     断言真 phase 转换（观测过 running）+ steps 来自 Argo nodes。
  B) cancel：sleep 300 长 wf → running → POST cancel → 轮询到 cancelled。
  C) resume（软断言）：suspend wf → POST resume → 200 不报错。
  D) logs(SSE)：succeeded wf → GET logs → body 含 busybox 真输出。

退出码：0 OK / 1 assert fail / 2 env unavailable。
"""

import json
import sys
import time
import urllib.error
import urllib.request

APISIX_URL = "http://127.0.0.1:30080"
DEMO_KEY = "ak_test_a_demo001"
TENANT_ID = "tenant_a"
WF_API_ID = "smoke-wf-api"
POLL_TIMEOUT = 150  # Argo 起 pod + 拉镜像，留足
POLL_INTERVAL = 3


def http(method, url, headers=None, data=None, timeout=30):
    req = urllib.request.Request(url, method=method, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def sh(cmd):
    import subprocess

    return subprocess.run(cmd, shell=True, capture_output=True, text=True, check=True).stdout


def seed_wf_api():
    sql = f"""
    INSERT INTO api (id, tenant_id, name, description, category, base_path, tags, status, visibility)
    VALUES ('{WF_API_ID}', '{TENANT_ID}', 'smoke wf argo', 'real argo e2e', 'smoke',
            '/smoke-wf-argo', ARRAY['smoke'], 'published', 'tenant')
    ON CONFLICT (id) DO NOTHING;
    """
    with open("/tmp/_wf_argo_seed.sql", "w") as f:
        f.write(sql)
    sh(
        "docker exec -i apihub-pg psql -U apihub -d apihub -v ON_ERROR_STOP=1 < /tmp/_wf_argo_seed.sql"
    )


def submit(spec):
    body = {"api_id": WF_API_ID, "app_id": "app_trading", "spec": spec}
    st, raw = http(
        "POST",
        f"{APISIX_URL}/v1/jobs",
        headers={"X-API-Key": DEMO_KEY, "Content-Type": "application/json"},
        data=json.dumps(body).encode(),
    )
    print(f"  POST /v1/jobs -> HTTP {st} {raw[:200]!r}")
    assert st == 201, f"POST /v1/jobs HTTP {st}: {raw}"
    wf = json.loads(raw)
    assert "id" in wf and "status" in wf, wf
    return wf["id"]


def poll(wf_id, want_statuses, timeout=POLL_TIMEOUT):
    """轮询到 status ∈ want_statuses；返回 (final_status, seen_statuses_set, last_detail)。"""
    seen = set()
    deadline = time.time() + timeout
    detail = {}
    while time.time() < deadline:
        st, raw = http("GET", f"{APISIX_URL}/v1/jobs/{wf_id}", headers={"X-API-Key": DEMO_KEY})
        assert st == 200, f"GET /v1/jobs/{wf_id} HTTP {st}: {raw}"
        detail = json.loads(raw)
        status = detail.get("status")
        if status:
            seen.add(status)
        if status in want_statuses:
            return status, seen, detail
        time.sleep(POLL_INTERVAL)
    raise AssertionError(f"poll timeout: wf={wf_id} want={want_statuses} seen={seen} last={detail}")


def main():
    print("== seed tenant_a workflow api ==")
    seed_wf_api()

    # ---- A) succeeded 主路径 ----
    print("== A) submit real Argo wf (busybox echo, SA=argo-exec) ==")
    spec_a = {
        "serviceAccountName": "argo-exec",
        "entrypoint": "main",
        "templates": [
            {
                "name": "main",
                "steps": [
                    [{"name": "s1", "template": "echo"}],
                    [{"name": "s2", "template": "echo"}],
                ],
            },
            {
                "name": "echo",
                "container": {
                    "image": "busybox:latest",
                    "command": ["sh", "-c", "echo hi from argo"],
                    "imagePullPolicy": "IfNotPresent",
                },
            },
        ],
    }
    wf_a = submit(spec_a)
    status, seen, detail = poll(wf_a, {"succeeded", "failed"})
    print(f"  final status={status} seen={sorted(seen)} steps={len(detail.get('steps') or [])}")
    assert status == "succeeded", f"A) 未成功: status={status} detail={detail}"
    # 真转换：观测过 running（区别 stub 瞬时）；首轮即 succeeded 也接受
    if seen != {"succeeded"}:
        assert "running" in seen, f"A) 未观测到 running 真转换: seen={seen}"
    # steps 真实：非空且含 succeeded node
    steps = detail.get("steps") or []
    assert len(steps) >= 1, f"A) steps 空: {detail}"
    assert any(s.get("status") == "succeeded" for s in steps), f"A) 无 succeeded step: {steps}"
    print(f"WORKFLOW-A OK —— wf={wf_a} succeeded, {len(steps)} real Argo nodes")

    # ---- B) cancel ----
    print("== B) submit long wf (sleep 300) → cancel ==")
    spec_b = {
        "serviceAccountName": "argo-exec",
        "entrypoint": "main",
        "templates": [
            {"name": "main", "steps": [[{"name": "long", "template": "sleep"}]]},
            {
                "name": "sleep",
                "container": {
                    "image": "busybox:latest",
                    "command": ["sleep", "300"],
                    "imagePullPolicy": "IfNotPresent",
                },
            },
        ],
    }
    wf_b = submit(spec_b)
    poll(wf_b, {"running", "succeeded"}, timeout=60)
    st, raw = http("POST", f"{APISIX_URL}/v1/jobs/{wf_b}/cancel", headers={"X-API-Key": DEMO_KEY})
    print(f"  POST /v1/jobs/{wf_b}/cancel -> HTTP {st} {raw[:120]!r}")
    assert st == 200, f"B) cancel HTTP {st}: {raw}"
    status_b, _, _ = poll(wf_b, {"cancelled", "failed", "succeeded"})
    print(f"  cancel final status={status_b}")
    # Argo Stop 映射 cancelled；容忍已先 succeeded（极端快）
    assert status_b in {"cancelled", "succeeded"}, f"B) cancel 后状态异常: {status_b}"
    print(f"WORKFLOW-B OK —— wf={wf_b} cancel→{status_b}")

    # ---- C) resume（软断言）----
    print("== C) submit suspend wf → resume（软断言）==")
    spec_c = {
        "serviceAccountName": "argo-exec",
        "entrypoint": "main",
        "templates": [
            {
                "name": "main",
                "steps": [
                    [{"name": "gate", "template": "hold"}],
                    [{"name": "fin", "template": "echo2"}],
                ],
            },
            {"name": "hold", "suspend": {}},
            {
                "name": "echo2",
                "container": {
                    "image": "busybox:latest",
                    "command": ["sh", "-c", "echo resumed"],
                    "imagePullPolicy": "IfNotPresent",
                },
            },
        ],
    }
    wf_c = submit(spec_c)
    poll(wf_c, {"running", "succeeded"}, timeout=60)
    st, raw = http("POST", f"{APISIX_URL}/v1/jobs/{wf_c}/resume", headers={"X-API-Key": DEMO_KEY})
    print(f"  POST /v1/jobs/{wf_c}/resume -> HTTP {st} {raw[:120]!r}")
    c_warning = ""
    if st != 200:
        # 软断言（brief：放宽可改 warning）—— resume 走 argo_client.py，当前对 Argo v3.0.x
        # 打的是 CRD 子资源 POST .../workflows/{name}/resume，而 v3.0.3 的 CRD 不注册该
        # 子资源（subresources={}）→ 404/502。真 resume 须经 argo-server API。属 Task 1
        # argo_client 缺陷，不在此 smoke 修复范围；记 warning，不阻断 A/B/D。
        c_warning = (
            f"C) resume HTTP {st}（软断言 warning）：argo_client.resume 打 CRD 子资源，"
            f"Argo v3.0.x 无 resume 子资源，须改走 argo-server —— 视为 Task 1 follow-up"
        )
        print(f"WORKFLOW-C WARNING —— wf={wf_c} {c_warning}")
    else:
        status_c, _, _ = poll(wf_c, {"succeeded", "failed"})
        print(f"  resume final status={status_c}")
        # 软断言：resume 200 + wf 走向 succeeded（suspend 解除后 echo2 跑完）
        if status_c != "succeeded":
            c_warning = f"C) resume 后未 succeeded（软断言 warning）: {status_c}"
            print(f"WORKFLOW-C WARNING —— wf={wf_c} {c_warning}")
        else:
            print(f"WORKFLOW-C OK —— wf={wf_c} resume→{status_c}")

    # ---- D) logs(SSE) ----
    print("== D) GET logs for succeeded wf A ==")
    st, raw = http(
        "GET", f"{APISIX_URL}/v1/jobs/{wf_a}/logs", headers={"X-API-Key": DEMO_KEY}, timeout=30
    )
    print(f"  GET /v1/jobs/{wf_a}/logs -> HTTP {st} {raw[:200]!r}")
    assert st == 200, f"D) logs HTTP {st}: {raw}"
    assert b"hi from argo" in raw, f"D) logs 不含 busybox 真输出: {raw!r}"
    print("WORKFLOW-D OK —— logs 含 'hi from argo'")

    if c_warning:
        print(f"ALL OK (with warning) —— A/B/D real Argo green; {c_warning}")
    else:
        print("ALL OK —— real Argo e2e green")
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"SMOKE FAIL: {e}")
        sys.exit(1)
    except (OSError, RuntimeError) as e:
        print(f"SMOKE ENV-UNAVAILABLE: {e}")
        sys.exit(2)
