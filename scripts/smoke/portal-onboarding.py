#!/usr/bin/env python3
"""外部开发者身份地基端到端 smoke。

链路：portal-bff /v1/portal/auth/* → auth（PG/Redis）→ 拿 JWT →
      portal-bff /v1/portal/apps → 拿 Key → APISIX /dispatch/smoke-sync/echo → 200 →
      portal-bff /v1/portal/apis 目录搜索 → portal-bff /v1/portal/try 在线调试。

前置：make dev-up + make run-auth + make run-portal + make run-dispatcher（或 kind 全栈）。
退出码：0 OK / 1 assert fail / 2 env unavailable。
"""

import json
import secrets
import sys
import urllib.error
import urllib.request

PORTAL_URL = "http://127.0.0.1:8011"
APISIX_URL = "http://127.0.0.1:30080"
PUBLIC_API_PATH = "/smoke-sync/echo"  # smoke-sync base_path=/smoke-sync, version path=/echo


def http(method, url, headers=None, data=None, timeout=15):
    req = urllib.request.Request(url, method=method, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def main():
    email = f"smoke_{secrets.token_hex(4)}@example.com"

    print("== ① 注册 ==")
    st, body = http(
        "POST",
        f"{PORTAL_URL}/v1/portal/auth/register",
        headers={"Content-Type": "application/json"},
        data=json.dumps(
            {"email": email, "password": "smoke1234", "phone": "13800000000", "name": "Smoke"}
        ).encode(),
    )
    print(f"  register -> HTTP {st} {body[:120]!r}")
    assert st == 201, f"register HTTP {st}: {body}"
    verify_token = json.loads(body)["verify_token"]

    print("== ② 邮箱验证（dev stub token）==")
    st, body = http("GET", f"{PORTAL_URL}/v1/portal/auth/verify-email?token={verify_token}")
    print(f"  verify -> HTTP {st} {body[:120]!r}")
    assert st == 200 and json.loads(body)["status"] == "active", f"verify HTTP {st}: {body}"

    print("== ③ 登录拿 JWT ==")
    st, body = http(
        "POST",
        f"{PORTAL_URL}/v1/portal/auth/login",
        headers={"Content-Type": "application/json"},
        data=json.dumps({"email": email, "password": "smoke1234"}).encode(),
    )
    print(f"  login -> HTTP {st}")
    assert st == 200, f"login HTTP {st}: {body}"
    token = json.loads(body)["access_token"]
    auth_hdr = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    print("== ④ 建应用 ==")
    st, body = http(
        "POST",
        f"{PORTAL_URL}/v1/portal/apps",
        headers=auth_hdr,
        data=json.dumps({"name": "smoke app", "type": "external"}).encode(),
    )
    print(f"  create app -> HTTP {st} {body[:120]!r}")
    assert st == 201, f"create app HTTP {st}: {body}"
    app_id = json.loads(body)["id"]

    print("== ⑤ 拿 API Key ==")
    st, body = http(
        "POST",
        f"{PORTAL_URL}/v1/portal/apps/{app_id}/api-keys",
        headers=auth_hdr,
        data=json.dumps({"name": "default"}).encode(),
    )
    print(f"  create key -> HTTP {st}")
    assert st == 201, f"create key HTTP {st}: {body}"
    api_key = json.loads(body)["api_key"]

    print("== ⑥ 用 Key 经 APISIX 调 smoke-sync(public) ==")
    st, body = http(
        "GET", f"{APISIX_URL}/dispatch{PUBLIC_API_PATH}", headers={"X-API-Key": api_key}
    )
    print(f"  call public API -> HTTP {st} {body[:120]!r}")
    assert st == 200, f"call public API HTTP {st}: {body}"

    print("== ⑦ API 目录搜索 ==")
    st, body = http("GET", f"{PORTAL_URL}/v1/portal/apis?search=smoke&limit=5", headers=auth_hdr)
    data = json.loads(body)
    print(f"  search -> HTTP {st}, total={data['total']}")
    assert st == 200, f"目录搜索失败: {st} {body}"
    assert data["total"] >= 1, f"应至少找到 1 个 API, 找到 {data['total']}"
    smoke_api = [a for a in data["items"] if "smoke" in a["name"].lower()]
    assert len(smoke_api) >= 1, f"应找到 smoke-sync API: {data['items']}"
    api_id = smoke_api[0]["api_id"]
    print(f"  找到 smoke-sync API: {api_id}")

    print("== ⑧ 在线调试 try ==")
    st, body = http(
        "POST",
        f"{PORTAL_URL}/v1/portal/try",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        data=json.dumps(
            {
                "api_id": api_id,
                "method": "POST",
                "body": {"message": "hello"},
                "api_key": api_key,
            }
        ).encode(),
    )
    try_data = json.loads(body)
    print(
        f"  try -> HTTP {st}, status={try_data.get('status')}, latency={try_data.get('latency_ms')}ms"
    )
    assert st == 200, f"try 端点返回非 200: {st} {body}"
    assert try_data.get("status") == 200, f"后端返回非 200: {try_data}"
    assert try_data.get("latency_ms", -1) >= 0, f"缺少 latency_ms: {try_data}"
    assert try_data.get("error") is None, f"有 error: {try_data}"

    print("== ⑨ 查用量统计 ==")
    st, body = http("GET", f"{PORTAL_URL}/v1/portal/usage", headers=auth_hdr)
    usage = json.loads(body)
    print(
        f"  usage -> HTTP {st}, plan={usage.get('plan', {}).get('code')}, calls={usage.get('total_calls')}"
    )
    assert st == 200
    assert usage.get("plan", {}).get("code") in ("free", "starter", "pro", "enterprise")

    print("PORTAL-ONBOARDING OK —— 外部开发者端到端闭环 + API 目录 + 在线调试 + 用量统计")
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
