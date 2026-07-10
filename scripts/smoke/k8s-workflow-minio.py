#!/usr/bin/env python3
"""MinIO 产物往返 smoke（kind，真 Argo）。

Argo 原生 artifact：step produce 写 /tmp/out.txt 并 outputs.artifacts →
Argo executor 经 artifactRepository(MinIO) PUT；step consume inputs.artifacts
接收并校验内容一致。

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
POLL_TIMEOUT = 150
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
    with open("/tmp/_wf_argo_seed_minio.sql", "w") as f:
        f.write(sql)
    sh(
        "docker exec -i apihub-pg psql -U apihub -d apihub -v ON_ERROR_STOP=1 < /tmp/_wf_argo_seed_minio.sql"
    )


def poll(wf_id, want, timeout=POLL_TIMEOUT):
    deadline = time.time() + timeout
    detail = {}
    while time.time() < deadline:
        st, raw = http("GET", f"{APISIX_URL}/v1/jobs/{wf_id}", headers={"X-API-Key": DEMO_KEY})
        assert st == 200, f"GET HTTP {st}: {raw}"
        detail = json.loads(raw)
        if detail.get("status") in want:
            return detail["status"], detail
        time.sleep(POLL_INTERVAL)
    raise AssertionError(f"poll timeout wf={wf_id} want={want} last={detail}")


def main():
    print("== seed tenant_a workflow api ==")
    seed_wf_api()

    spec = {
        "serviceAccountName": "argo-exec",
        "entrypoint": "main",
        "templates": [
            {
                "name": "main",
                "steps": [
                    [{"name": "produce", "template": "produce"}],
                    # Argo step 间 artifact 不会按名字自动贯通：consume 模板声明了
                    # inputs.artifacts.out，须由调用处 arguments.artifacts.from 显式接到
                    # produce 步骤的 outputs.artifacts.out（executor 仍经 MinIO 上传/下载）
                    [
                        {
                            "name": "consume",
                            "template": "consume",
                            "arguments": {
                                "artifacts": [
                                    {
                                        "name": "out",
                                        "from": "{{steps.produce.outputs.artifacts.out}}",
                                    }
                                ]
                            },
                        }
                    ],
                ],
            },
            {
                "name": "produce",
                "container": {
                    "image": "busybox:latest",
                    # emissary executor（v3.5 默认）无 PNS 捕获竞态——亚秒容器退出即捕获产物，
                    # 不再需要 v3.0.3/pns 时代的 `sleep 2` 让 main 活片刻等 executor secure rootfs。
                    "command": ["sh", "-c", "echo -n artifact-content-xyz > /tmp/out.txt"],
                    # kind 节点无 egress；:latest 默认 Always 拉会失败 → 必须 IfNotPresent（T6 教训）
                    "imagePullPolicy": "IfNotPresent",
                },
                "outputs": {"artifacts": [{"name": "out", "path": "/tmp/out.txt"}]},
            },
            {
                "name": "consume",
                "inputs": {"artifacts": [{"name": "out", "path": "/tmp/in.txt"}]},
                "container": {
                    "image": "busybox:latest",
                    # 读入 artifact，内容不符则 exit 1 → Argo 标 node Failed → wf failed
                    "command": [
                        "sh",
                        "-c",
                        "cat /tmp/in.txt; echo; "
                        "grep -q artifact-content-xyz /tmp/in.txt || "
                        "(echo MISMATCH && exit 1)",
                    ],
                    "imagePullPolicy": "IfNotPresent",
                },
            },
        ],
    }

    body = {"api_id": WF_API_ID, "app_id": "app_trading", "spec": spec}
    st, raw = http(
        "POST",
        f"{APISIX_URL}/v1/jobs",
        headers={"X-API-Key": DEMO_KEY, "Content-Type": "application/json"},
        data=json.dumps(body).encode(),
    )
    print(f"  POST /v1/jobs -> HTTP {st} {raw[:200]!r}")
    assert st == 201, f"POST HTTP {st}: {raw}"
    wf_id = json.loads(raw)["id"]

    status, detail = poll(wf_id, {"succeeded", "failed"})
    print(f"  final status={status} steps={len(detail.get('steps') or [])}")
    # consume 校验失败会变 failed —— succeeded 即证明产物经 MinIO 往返一致
    assert (
        status == "succeeded"
    ), f"MinIO artifact 往返失败（consume 校验不过 / Argo artifact error）: {detail}"

    # 软断言：直查 MinIO bucket 存在对象（Argo 产物 key 随机，只验 bucket 非空）
    try:
        out = sh(
            "docker exec apihub-minio mc ls --recursive local/argo-artifacts/ 2>/dev/null | head"
        )
        print(f"  [soft] minio argo-artifacts listing:\n{out or '  (空或 mc 未配)'}")
    except Exception as e:  # noqa: BLE001
        print(f"  [soft] minio 直查跳过: {e}")

    print(f"WORKFLOW-MINIO OK —— wf={wf_id} succeeded, artifact 经 MinIO 往返一致")
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
