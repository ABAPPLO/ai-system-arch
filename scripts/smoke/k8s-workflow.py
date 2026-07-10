#!/usr/bin/env python3
"""workflow stub e2e smoke（kind）。

经 APISIX → dispatcher /v1/jobs → workflow-svc（stub）：
  1) seed 一条 tenant_a 的 api（供 workflow_instance 引用）
  2) POST /v1/jobs 带 2-template Argo spec → 断言 201 + workflow_id + status
  3) GET /v1/jobs/{id} → 断言 200 + status running + steps 非空

退出码：0 OK / 1 assert fail / 2 env unavailable。
"""

import json
import sys
import urllib.error
import urllib.request

APISIX_URL = "http://127.0.0.1:30080"
DEMO_KEY = "ak_test_a_demo001"
TENANT_ID = "tenant_a"
WF_API_ID = "smoke-wf-api"


def http(method, url, headers=None, data=None, timeout=20):
    req = urllib.request.Request(url, method=method, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def sh(cmd):
    import subprocess

    return subprocess.run(cmd, shell=True, capture_output=True, text=True, check=True).stdout


def seed_wf_api():
    # 走 apihub 超管插入：api 表带 RLS WITH CHECK，app 角色无 tenant 上下文会被拒
    # （与 Task 1/2 的 smoke 同款做法）。
    sql = f"""
    INSERT INTO api (id, tenant_id, name, description, category, base_path, tags, status, visibility)
    VALUES ('{WF_API_ID}', '{TENANT_ID}', 'smoke wf', 'wf e2e', 'smoke',
            '/smoke-wf', ARRAY['smoke'], 'published', 'tenant')
    ON CONFLICT (id) DO NOTHING;
    """
    with open("/tmp/_wf_seed.sql", "w") as f:
        f.write(sql)
    sh("docker exec -i apihub-pg psql -U apihub -d apihub -v ON_ERROR_STOP=1 < /tmp/_wf_seed.sql")


def main():
    print("== seed tenant_a workflow api ==")
    seed_wf_api()

    spec = {
        "entrypoint": "main",
        "templates": [
            {
                "name": "main",
                "steps": [
                    [{"name": "s1", "template": "echo"}],
                    [{"name": "s2", "template": "echo"}],
                ],
            },
            {"name": "echo", "container": {"image": "busybox", "command": ["echo", "hi"]}},
        ],
    }
    body = {"api_id": WF_API_ID, "app_id": "app_trading", "spec": spec}

    print("== POST /v1/jobs via APISIX → dispatcher → workflow-svc ==")
    st, resp = http(
        "POST",
        f"{APISIX_URL}/v1/jobs",
        headers={"X-API-Key": DEMO_KEY, "Content-Type": "application/json"},
        data=json.dumps(body).encode(),
    )
    print(f"  POST /v1/jobs -> HTTP {st} {resp[:200]!r}")
    if st == 502 and "verify" in resp.lower():
        print(
            "  [diag] 502 含鉴权错误 —— 多半是 workflow-svc 收到的 X-API-Key 无效或 tenant 不符；查 dispatcher 透传 + workflow-svc tenant middleware"
        )
    assert st == 201, f"POST /v1/jobs HTTP {st}: {resp}"
    wf = json.loads(resp)
    assert "id" in wf and "status" in wf, wf
    wf_id = wf["id"]

    print(f"== GET /v1/jobs/{wf_id} ==")
    st, resp = http("GET", f"{APISIX_URL}/v1/jobs/{wf_id}", headers={"X-API-Key": DEMO_KEY})
    print(f"  GET /v1/jobs/{wf_id} -> HTTP {st} {resp[:200]!r}")
    assert st == 200, f"GET HTTP {st}: {resp}"
    detail = json.loads(resp)
    assert detail.get("status") == "running", detail
    steps = detail.get("steps") or []
    assert len(steps) >= 1, f"steps 空: {detail}"
    print(
        f"WORKFLOW OK —— workflow_id={wf_id} status=running steps={[s.get('name') for s in steps]}"
    )
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
