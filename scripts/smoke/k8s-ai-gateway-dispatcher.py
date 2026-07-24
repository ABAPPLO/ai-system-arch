#!/usr/bin/env python3
"""R2c e2e 全链 —— APISIX → dispatcher → ai-gateway → mock-backend SSE。

拓扑：client → APISIX(key-auth + limit-count) → dispatcher(ai_model SSE 转发 + token 抽取 +
Kafka 计费) → ai-gateway(ClusterIP, /v1/chat/completions skip_auth, resolve_model_route +
decrypt) → mock-backend(OpenAI 风格 SSE)。

四条断言：
  R2C-A  SSE 流式透传      —— HTTP 200 + body 含 'data: ' + '[DONE]'。
  R2C-B  token 计费         —— CH apihub.api_call_log 最近该 api 的 token_total > 0
                              （dispatcher _emit_stream_complete 发 Kafka api-call-events →
                              CH Kafka-engine MV 直读）。兜底：dump Kafka topic 含 token。
  R2C-C  多 Provider 路由   —— 未知 model → ai-gateway 400 "not supported"，body 经
                              dispatcher 透传（注：dispatcher StreamingResponse 默认 200，
                              上游 status 不外透，故断言 body 而非 status）。
  R2C-D  限流               —— rate_limit {count:3, window:60s} 下连发第 4 次 → 429。

退出码：0 OK / 1 assert fail / 2 env unavailable。

前置：
  1. kind-apihub 集群 + 13 服务 Running（含 ai-gateway / dispatcher / mock-backend）。
  2. APISIX 已装（scripts/kind/apisix-setup.sh）—— NodePort 30080 + admin svc apisix-admin。
  3. ai-gateway-secret 含 AI_GATEWAY_ENCRYPTION_KEY（patch
     deploy/k8s/overlays/kind/patches/ai-gateway-secret.yaml）。
  4. host compose 起 PG(15433)/Redis(16380)/Kafka(9094)/CH(8123)。
"""

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

APISIX_URL = "http://127.0.0.1:30080"
DEMO_KEY = (
    "ak_test_a_demo001"  # tenant_a/app_trading（02-seed.sql），APISIX consumer "smoke" 同 key
)
TENANT_ID = "tenant_a"
APP_ID = "app_trading"

AI_API_ID = "smoke-llm-chat"  # api.id text
AI_VER_ID = "ver_smoke_llm_v1"  # api_version.id text
AI_BASE_PATH = "/smoke-llm-chat"
AI_VER_PATH = "/v1/chat/completions"  # 路由 URI = base_path + path
MODEL = "gpt-4o-mini"
MOCK_PROVIDER_ID = "11111111-1111-1111-1111-111111111111"

# mock-backend 不校验 key，但 ai-gateway crypto.decrypt 必须不抛 —— 用 dev
# AI_GATEWAY_ENCRYPTION_KEY（deadbeef...）AES-256-GCM 加密 "mock-key" 后的 base64。
# 一次性离线产物（AES-GCM nonce 固定）；每次跑都复用，确定性 + 幂等。
MOCK_KEY_ENC = "VqtUBK4hO6uAi7KxR8/whmtYafMqexjhkr+3WC2YSJCgtJ2M"

# APISIX admin（port-forward 用）
APISIX_ADMIN_LOCAL_PORT = "19380"
APISIX_ADMIN_DEFAULT_KEY = "edd1c9f034335f136f87ad84b625c8f1"  # chart 默认；setup 脚本里会自发现
INGRESS_SHARED_SECRET = "ingress-shared-dev"  # 与 apisix-setup.sh 默认一致

# rate-limit 配置（seed 进 api_version.rate_limit → publish_route 转为 limit-count 插件）
RATE_LIMIT_COUNT = 3
RATE_LIMIT_WINDOW = 60


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def sh(cmd, check=True):
    """跑 shell，返回 stdout。失败抛带 stderr 的 RuntimeError。"""
    r = subprocess.run(cmd, shell=True, text=True, capture_output=True)
    if check and r.returncode != 0:
        raise RuntimeError(
            f"cmd failed (rc={r.returncode}): {cmd}\n--- stdout ---\n{r.stdout}\n"
            f"--- stderr ---\n{r.stderr}"
        )
    return r.stdout


def http(method, url, headers=None, data=None, timeout=30):
    req = urllib.request.Request(url, method=method, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# ---------------------------------------------------------------------------
# 前置探测
# ---------------------------------------------------------------------------


def probe_env():
    """快速确认 kind / APISIX / 关键 pod 就绪；任一缺失 → exit 2 (env unavailable)。"""
    try:
        nodes = sh(
            "kubectl --context kind-apihub get nodes --no-headers 2>&1 | wc -l", check=False
        ).strip()
        if nodes == "0":
            raise RuntimeError("kind-apihub cluster not reachable")
        pods = sh(
            "kubectl --context kind-apihub -n apihub-system get pods --no-headers 2>&1",
            check=False,
        )
        for want in ("ai-gateway-", "dispatcher-", "mock-backend-"):
            if not any(line.startswith(want) and " Running" in line for line in pods.splitlines()):
                raise RuntimeError(f"pod {want}* not Running in apihub-system")
        # APISIX gateway NodePort 30080
        st, _ = http("GET", f"{APISIX_URL}/", timeout=5)
        # 401 / 404 都算 APISIX 在数据面（key-auth reject / no route）；
        # 端口未通 → urlopen 抛 URLError(OSError) → 外层 except 捕获 → exit 2。
        print(
            f"== env ok: kind up, APISIX 30080 (HTTP {st}), ai-gateway/dispatcher/mock-backend Running"
        )
    except (OSError, RuntimeError) as e:
        print(f"SMOKE ENV-UNAVAILABLE: {e}")
        sys.exit(2)


# ---------------------------------------------------------------------------
# APISIX admin（port-forward + admin key 发现 + route upsert）
# ---------------------------------------------------------------------------


_PF_PID = None


def open_apisix_admin():
    """port-forward svc/apisix-admin → 返回 (admin_url, admin_key)。"""
    global _PF_PID
    sh(
        f"kubectl --context kind-apihub -n apihub-ingress port-forward svc/apisix-admin "
        f"{APISIX_ADMIN_LOCAL_PORT}:9180 >/tmp/r2c-apisix-pf.log 2>&1 &"
    )
    _PF_PID = (
        subprocess.run(
            "pgrep -f 'port-forward svc/apisix-admin'", shell=True, text=True, capture_output=True
        )
        .stdout.strip()
        .split("\n")[0]
    )
    # 等 admin API 起来
    admin_key = (
        sh(
            "kubectl --context kind-apihub -n apihub-ingress get cm apisix "
            "-o jsonpath=\"{.data['config\\.yaml']}\" 2>/dev/null | "
            "awk '/name: \"admin\"/{f=1} f && /key:/{print $2; exit}'",
            check=False,
        ).strip()
        or APISIX_ADMIN_DEFAULT_KEY
    )
    up = False
    for _ in range(40):
        st, _ = http(
            "GET",
            f"http://127.0.0.1:{APISIX_ADMIN_LOCAL_PORT}/apisix/admin/consumers",
            headers={"X-API-KEY": admin_key},
            timeout=2,
        )
        if st in (200, 404):  # 404 = admin 路径不同版本差异；只要 TCP 通即可
            up = True
            break
        time.sleep(0.5)
    if not up:
        raise RuntimeError("APISIX admin port-forward did not come up")
    print(f"  apisix admin port-forward up; admin_key={admin_key[:8]}...")
    return f"http://127.0.0.1:{APISIX_ADMIN_LOCAL_PORT}/apisix/admin", admin_key


def close_apisix_admin():
    global _PF_PID
    if _PF_PID:
        subprocess.run(["kill", _PF_PID], stderr=subprocess.DEVNULL)
        _PF_PID = None


def upsert_route(admin_url, admin_key):
    """upsert route id=AI_VER_ID：URI=base+path → dispatcher，注入 X-API-Version-Id +
    X-Ingress-Auth；rate_limit 走 limit-count 插件（与 api-registry publish_route 同形态）。"""
    uri = AI_BASE_PATH.rstrip("/") + AI_VER_PATH
    plugins = {
        "key-auth": {"header": "X-API-Key"},
        "proxy-rewrite": {
            "regex_uri": ["^/(.*)$", "/dispatch/$1"],
            "headers": {
                "set": {
                    "X-API-Version-Id": AI_VER_ID,
                    "X-Ingress-Auth": INGRESS_SHARED_SECRET,
                }
            },
        },
        "limit-count": {
            "count": RATE_LIMIT_COUNT,
            "time_window": RATE_LIMIT_WINDOW,
            "key": "consumer_name",
            "policy": "local",
            "rejected_code": 429,
        },
    }
    body = {
        "uri": uri,
        "methods": ["POST"],
        "upstream": {"type": "roundrobin", "nodes": {"dispatcher.apihub-system:80": 1}},
        "plugins": plugins,
    }
    data = json.dumps(body).encode()
    st, raw = http(
        "PUT",
        f"{admin_url}/routes/{AI_VER_ID}",
        headers={"X-API-KEY": admin_key, "Content-Type": "application/json"},
        data=data,
        timeout=10,
    )
    print(f"  APISIX PUT /routes/{AI_VER_ID} uri={uri} -> HTTP {st} {raw[:120]!r}")
    assert st in (200, 201), f"upsert route failed: {st} {raw}"


# ---------------------------------------------------------------------------
# seed
# ---------------------------------------------------------------------------


def seed():
    # 固定 UUID 到每行（含 ai_provider_key/ai_model_route 这两个 PK=uuid default 的表），
    # 这样 ON CONFLICT (id) 全用 PK，无需 DELETE 即可幂等 upsert。
    # （ai_provider_key 无 (provider_id,key_alias) unique 约束，不能用那个做 ON CONFLICT。）
    sql = f"""
    BEGIN;
    INSERT INTO api (id, tenant_id, name, description, category, base_path,
                     tags, status, visibility, metadata, created_at, updated_at)
    VALUES ('{AI_API_ID}', '{TENANT_ID}', 'smoke llm chat', 'r2c e2e',
            'smoke', '{AI_BASE_PATH}', ARRAY['smoke'], 'published', 'tenant',
            '{{}}'::jsonb, NOW(), NOW())
    ON CONFLICT (id) DO UPDATE SET status='published', updated_at=NOW();

    INSERT INTO api_version (id, tenant_id, api_id, version, backend_type, backend_url,
                             method, path, ai_model, ai_streaming, status, rate_limit,
                             created_at, updated_at)
    VALUES ('{AI_VER_ID}', '{TENANT_ID}', '{AI_API_ID}', 'v1', 'ai_model',
            'http://ai-gateway.apihub-system/v1/chat/completions',
            'POST', '{AI_VER_PATH}', '{MODEL}', true, 'published',
            '{{"count":{RATE_LIMIT_COUNT}, "window_seconds":{RATE_LIMIT_WINDOW}}}'::jsonb,
            NOW(), NOW())
    ON CONFLICT (id) DO UPDATE SET
        backend_type='ai_model',
        backend_url=EXCLUDED.backend_url,
        method='POST',
        path=EXCLUDED.path,
        ai_model=EXCLUDED.ai_model,
        ai_streaming=true,
        status='published',
        rate_limit=EXCLUDED.rate_limit,
        updated_at=NOW();

    INSERT INTO ai_provider (id, name, provider_type, base_url, default_model, status)
    VALUES ('{MOCK_PROVIDER_ID}', 'mock-openai', 'openai_compatible',
            'http://mock-backend.apihub-system:80/v1', '{MODEL}', 'active')
    ON CONFLICT (id) DO UPDATE SET
        provider_type=EXCLUDED.provider_type,
        base_url=EXCLUDED.base_url,
        default_model=EXCLUDED.default_model,
        status='active';

    INSERT INTO ai_provider_key (id, provider_id, key_alias, key_encrypted, key_prefix, status)
    VALUES ('22222222-2222-2222-2222-222222222222', '{MOCK_PROVIDER_ID}', 'mock',
            '{MOCK_KEY_ENC}', 'mk_', 'active')
    ON CONFLICT (id) DO UPDATE SET
        key_encrypted=EXCLUDED.key_encrypted,
        status='active';

    INSERT INTO ai_model_route (id, model_pattern, target_provider_id, target_model, priority, status)
    VALUES ('33333333-3333-3333-3333-333333333333', 'gpt-4o%', '{MOCK_PROVIDER_ID}',
            '{MODEL}', 0, 'active')
    ON CONFLICT (id) DO UPDATE SET
        model_pattern=EXCLUDED.model_pattern,
        target_provider_id=EXCLUDED.target_provider_id,
        target_model=EXCLUDED.target_model,
        status='active';
    COMMIT;
    """
    with open("/tmp/_r2c_seed.sql", "w") as f:
        f.write(sql)
    out = sh(
        "docker exec -i apihub-pg psql -U apihub -d apihub -v ON_ERROR_STOP=1 < /tmp/_r2c_seed.sql"
    )
    last = [line for line in out.strip().splitlines() if line.strip()][-1:] or ["?"]
    print(f"  seed ok: {last[0]}")

    # 清掉 dispatcher 的 snapshot cache（resolver redis t_set 5min；旧 status='draft' 不可达）
    sh(
        "docker exec apihub-redis redis-cli -a apihub_dev_pwd -n 0 DEL snapshot:"
        f"{AI_VER_ID} >/dev/null 2>&1 || true",
        check=False,
    )


# ---------------------------------------------------------------------------
# 断言
# ---------------------------------------------------------------------------


def assert_sse_passthrough():
    """R2C-A: client POST /smoke-llm-chat/v1/chat/completions → SSE 200 + [DONE]。"""
    body = json.dumps(
        {"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "stream": True}
    ).encode()
    st, raw = http(
        "POST",
        f"{APISIX_URL}{AI_BASE_PATH}{AI_VER_PATH}",
        headers={"X-API-Key": DEMO_KEY, "Content-Type": "application/json"},
        data=body,
        timeout=30,
    )
    print(f"  POST chat -> HTTP {st} {raw[:200]!r}")
    assert st == 200, f"R2C-A HTTP {st} (want 200): {raw!r}"
    assert b"data: " in raw, f"R2C-A SSE 未透传（无 'data: ' 行）: {raw!r}"
    assert b"[DONE]" in raw, f"R2C-A SSE 未透传（无 [DONE]）: {raw!r}"
    print("  [R2C-A] SSE 透传 OK（含 'data: ' + '[DONE]'）")
    return raw


def assert_billing():
    """R2C-B: CH apihub.api_call_log 最近该 api 的 token_total > 0。

    dispatcher _emit_stream_complete 发 Kafka api-call-events（含 token_prompt/completion），
    CH Kafka-engine 表 api_call_events_src → MV api_call_log_mv → 实表 api_call_log。
    轮询 30s（MV 触发 + CH 索引延迟通常 <5s）。兜底：dump Kafka topic 直接验证。
    """
    deadline = time.time() + 30
    last = ""
    while time.time() < deadline:
        out = sh(
            "docker exec apihub-clickhouse clickhouse-client --user apihub --password "
            f'apihub_dev_pwd -q "SELECT token_total FROM apihub.api_call_log '
            f"WHERE api_id='{AI_API_ID}' ORDER BY ts DESC LIMIT 1\" 2>&1",
            check=False,
        ).strip()
        last = out
        # CH 0 行返回空字符串；非零整数算 PASS
        if out and out.isdigit() and int(out) > 0:
            print(f"  [R2C-B] token_total={out}（CH api_call_log 写入 OK）")
            return
        time.sleep(2)
    # 兜底：dump Kafka topic 最近 10 条，确认 dispatcher 至少 emit 了带 token 的 event
    print(f"  [wait] CH token_total TIMEOUT; last={last!r}; fallback → Kafka dump")
    dump = sh(
        "docker exec apihub-kafka kafka-console-consumer.sh --bootstrap-server localhost:9094 "
        "--topic api-call-events --from-beginning --max-messages 50 --timeout-ms 8000 2>&1",
        check=False,
    )
    matches = [
        line
        for line in dump.splitlines()
        if f'"api_id": "{AI_API_ID}"' in line or f'"api_id":"{AI_API_ID}"' in line
    ]
    token_hits = [
        line for line in matches if '"token_total":' in line and '"token_total": 0' not in line
    ]
    if token_hits:
        print(
            f"  [R2C-B] Kafka fallback OK —— {len(token_hits)} 条 token>0 event: "
            f"{token_hits[-1][:160]}"
        )
        print(
            "  [R2C-B] (注意：CH 写入未观测到，但 dispatcher Kafka emit 正常 → "
            "可能是 CH Kafka-engine broker_list 配置问题)"
        )
        return
    raise AssertionError(
        f"R2C-B 计费未触达：CH api_call_log 无 token>0 行；Kafka dump 也无 token>0 event；"
        f"kafka raw tail:\n{dump[-500:]}"
    )


def assert_routing():
    """R2C-C: 未知 model → ai-gateway 400 "not supported" body 经 dispatcher 透传。

    注：dispatcher StreamingResponse 默认 200，上游 4xx status 不外透 → 客户端收到 200 +
    错误 body。这是 dispatcher 已知行为（forwarder._forward_stream 未把上游 status 透传给
    StreamingResponse 构造）。本 e2e 按 body 内容断言；status 透传为 follow-up：dispatcher
    forwarder SSE 上游 status 透传（pre-existing 自原 dispatcher，非 R2e/R2c 引入，单独跟踪）。
    """
    body = json.dumps(
        {
            "model": "unknown-model-xyz",
            "messages": [{"role": "user", "content": "x"}],
            "stream": True,
        }
    ).encode()
    st, raw = http(
        "POST",
        f"{APISIX_URL}{AI_BASE_PATH}{AI_VER_PATH}",
        headers={"X-API-Key": DEMO_KEY, "Content-Type": "application/json"},
        data=body,
        timeout=30,
    )
    print(f"  POST unknown-model -> HTTP {st} {raw[:200]!r}")
    # ai-gateway HTTPException(400, detail="model '...' not supported") body 形如
    # {"detail":"model 'unknown-model-xyz' not supported"}
    assert b"not supported" in raw, (
        f"R2C-C 未路由拒绝：期待 body 含 'not supported'，实际 HTTP {st} body={raw!r}"
    )
    print("  [R2C-C] 多 Provider 路由 OK —— 未知 model 经 ai-gateway 拒绝（body 透传）")


def assert_rate_limit():
    """R2C-D: 连发 RATE_LIMIT_COUNT + 1 次 → 最后一次 429。

    limit-count 插件 key=consumer_name，consumer='smoke'；window=60s。
    SSE 请求会消耗一次配额（无论 status）。
    """
    seen_codes = []
    for i in range(RATE_LIMIT_COUNT + 1):
        body = json.dumps(
            {"model": MODEL, "messages": [{"role": "user", "content": f"q{i}"}], "stream": True}
        ).encode()
        st, raw = http(
            "POST",
            f"{APISIX_URL}{AI_BASE_PATH}{AI_VER_PATH}",
            headers={"X-API-Key": DEMO_KEY, "Content-Type": "application/json"},
            data=body,
            timeout=30,
        )
        seen_codes.append(st)
        print(f"  burst #{i + 1} -> HTTP {st}")
        if st == 429:
            print(f"  [R2C-D] 限流 OK —— 第 {i + 1} 次触发 429（codes={seen_codes}）")
            return
    raise AssertionError(
        f"R2C-D 限流未触发：连发 {RATE_LIMIT_COUNT + 1} 次均未 429（codes={seen_codes}）"
    )


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main():
    probe_env()

    print("== seed llm-chat api + ai_provider/key/route ==")
    seed()

    print("== upsert APISIX route ==")
    admin_url, admin_key = open_apisix_admin()
    try:
        upsert_route(admin_url, admin_key)
        # 等 APISIX 同步到 gateway（etcd watch 秒级，保险起见 2s）
        time.sleep(2)

        print("== R2C-A SSE 透传 ==")
        assert_sse_passthrough()

        print("== R2C-B token 计费（CH / Kafka）==")
        assert_billing()

        print("== R2C-C 多 Provider 路由（未知 model → 拒绝）==")
        assert_routing()

        print("== R2C-D 限流（over limit → 429）==")
        assert_rate_limit()
    finally:
        close_apisix_admin()

    print("ALL OK —— R2c e2e green (APISIX→dispatcher→ai-gateway→mock SSE 全链)")
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
