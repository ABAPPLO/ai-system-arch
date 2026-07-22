#!/usr/bin/env python3
"""R3a e2e —— Go quota 逐字段对照 Python 契约（kind）。

Go quota (services/go/quota) 取代 Python (services/services/quota) 后必须保留
Python `QuotaCheckResponse` / `UsageResponse` 的可观测契约，让 dispatcher /
api-registry 等 caller 零改动。本脚本直连 Go quota（port-forward svc/quota:80
+ 注入 `X-Ingress-Auth` header）逐字段验证。

五条断言：
  R3A-A  鉴权信任入口   —— 无 X-Ingress-Auth → 401；/health/live 旁路鉴权 → 200；
                            错 secret → 401；对 secret → 业务路 200。
  R3A-B  check 响应字段  —— POST /v1/quota/check 返回 JSON 字段集合 ==
                            {allowed, tier_blocked, limit, remaining,
                             retry_after_seconds, rule_source}（无 current /
                            reset_ms；tier_blocked/limit/remaining 可 null）。
                            T2: rule_source 必须 == "app"（seed app override
                            生效，证明 3 层 merge 加载了 PG seed）。
  R3A-C  usage 扁平结构  —— GET /v1/quota/usage JSON 顶层 == {tenant_id, app_id,
                            api_id, second, minute, day}；每个 point 字段 ==
                            {window_seconds, used, limit}。
                            T2: 三 tier limit 必须匹配 seed merge 结果
                            （second=20 app override, minute=100, day=1000 from
                            api_version floor）。
  R3A-D  Redis key 格式   —— check 后 redis-cli 出现 key 形如
                            `t:<tenant>:rate:<api>:<app>:{s|m|d}:<slot>`。
  R3A-E  Lua 原子性       —— burst 15 次并发 check（tenant_smoke_r3a_lua），
                            seeded tenant second max=5，断言 admitted == 5
                            （精确，证明规则来自 seed 不是 hardcoded default）。

退出码：0 OK / 1 assert fail / 2 env unavailable。

前置：
  1. kind-apihub 集群 + quota pod Running（已切 Go image）。
  2. host compose 起 PG(15433)/Redis(16380)（Go quota 通过 host.docker.internal
     访问；envFrom: apihub-shared-infra + quota-config）。
  3. quota-config CM 已含 INGRESS_SHARED_SECRET（与 dispatcher R1d 一致）。
"""

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

# --- cluster 常量 ---
NS = "apihub-system"
CTX = "kind-apihub"
QUOTA_PF_PORT = "18004"
QUOTA_URL = f"http://127.0.0.1:{QUOTA_PF_PORT}"

# --- 业务参数（与 dispatcher R1d / apisix-setup.sh INGRESS_SHARED_SECRET 一致）---
INGRESS_SHARED_SECRET = "ingress-shared-dev"

# --- e2e 用的 tenant/app/api 标识（必须与 13-quota-rules-seed.sql 一致）---
# T2 后：这些 ID 对应 PG 真行（rate_limit JSONB seed），证明 Go LoadRules 的
# 3 层 merge 端到端走通——不是仅 Redis key 维度了。
TENANT_ID = "tenant_smoke_r3a"
APP_ID = "app_smoke_r3a"
API_ID = "api_smoke_r3a"

# T2 seed-derived expectations（13-quota-rules-seed.sql）：
#   - app_smoke_r3a.rate_limit = {"second":{"max_count":20,...}} （app override）
#   - ver_smoke_r3a_v1.rate_limit second=10, minute=100, day=1000 （api_version floor）
#   - merged main path: second=20 (app wins) / minute=100 / day=1000, source="app"
APP_SECOND_MAX = 20
API_VERSION_MINUTE_MAX = 100
API_VERSION_DAY_MAX = 1000

# Lua 原子断言维度：tenant_smoke_r3a_lua（与主路径不同 tenant，隔离 second slot）。
# 该 tenant 仅有 tenant.rate_limit = second=5（13-quota-rules-seed.sql）。
# burst 15 个并发 → admitted 精确 5（seeded tenant 第二层上限），blocked 10。
# 5 是非默认值（历史 R3a defaultRules.Second.MaxCount=10 已被 T1 删除），
# 证明规则来自 seed JSONB 而非 hardcoded default。
LUA_TENANT_ID = "tenant_smoke_r3a_lua"
LUA_TENANT_SECOND_MAX = 5
LUA_BURST_N = 15


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


def auth_headers(extra=None):
    h = {"X-Ingress-Auth": INGRESS_SHARED_SECRET}
    if extra:
        h.update(extra)
    return h


# ---------------------------------------------------------------------------
# 前置探测
# ---------------------------------------------------------------------------


def probe_env():
    """快速确认 kind / quota pod Running；任一缺失 → exit 2 (env unavailable)。"""
    try:
        nodes = sh(
            f"kubectl --context {CTX} get nodes --no-headers 2>&1 | wc -l", check=False
        ).strip()
        if nodes == "0":
            raise RuntimeError(f"cluster {CTX} not reachable")
        pods = sh(
            f"kubectl --context {CTX} -n {NS} get pods -l app.kubernetes.io/name=quota "
            "--no-headers 2>&1",
            check=False,
        )
        if not any(" Running" in line for line in pods.splitlines()):
            raise RuntimeError("no Running quota pod")
        print(f"== env ok: {CTX} up, quota pod Running")
    except (OSError, RuntimeError) as e:
        print(f"SMOKE ENV-UNAVAILABLE: {e}")
        sys.exit(2)


# ---------------------------------------------------------------------------
# port-forward
# ---------------------------------------------------------------------------


_PF_PID = None


def open_quota_pf():
    """port-forward svc/quota:80 → QUOTA_PF_PORT；轮询 /health/ready=200 才返回。"""
    global _PF_PID
    sh(
        f"kubectl --context {CTX} -n {NS} port-forward svc/quota "
        f"{QUOTA_PF_PORT}:80 >/tmp/r3a-quota-pf.log 2>&1 &"
    )
    _PF_PID = (
        subprocess.run(
            "pgrep -f 'port-forward svc/quota'", shell=True, text=True, capture_output=True
        )
        .stdout.strip()
        .split("\n")[0]
    )
    deadline = time.time() + 30
    last_err = ""
    while time.time() < deadline:
        try:
            st, _ = http("GET", f"{QUOTA_URL}/health/ready", timeout=2)
        except urllib.error.URLError as e:
            # port-forward 还在建立 listener；轮询重试，不当 env-unavailable
            last_err = str(e)
            time.sleep(0.5)
            continue
        if st == 200:
            print(f"  quota port-forward up (pid={_PF_PID}); /health/ready=200")
            return
        last_err = f"HTTP {st}"
        time.sleep(0.5)
    raise RuntimeError(f"quota port-forward did not come up (last={last_err})")


def close_quota_pf():
    global _PF_PID
    if _PF_PID:
        subprocess.run(["kill", _PF_PID], stderr=subprocess.DEVNULL)
        _PF_PID = None


# ---------------------------------------------------------------------------
# 断言
# ---------------------------------------------------------------------------


def assert_auth():
    """R3A-A: 信任入口鉴权。
    - /health/live 200（无鉴权）
    - /v1/quota/check 无 X-Ingress-Auth → 401
    - /v1/quota/check 错 secret → 401
    - /v1/quota/check 对 secret + invalid body → 400（说明过鉴权到了 handler）
    """
    st_live, _ = http("GET", f"{QUOTA_URL}/health/live", timeout=3)
    print(f"  GET /health/live (no auth) -> {st_live}")
    assert st_live == 200, f"R3A-A live 想要 200，实际 {st_live}"

    # 无 header
    st_no, raw_no = http(
        "POST",
        f"{QUOTA_URL}/v1/quota/check",
        headers={"Content-Type": "application/json"},
        data=b'{"tenant_id":"x","app_id":"y","api_id":"z"}',
        timeout=5,
    )
    print(f"  POST /v1/quota/check (no X-Ingress-Auth) -> {st_no} {raw_no!r}")
    assert st_no == 401, f"R3A-A 无 secret 想要 401，实际 {st_no}"

    # 错 secret
    st_bad, raw_bad = http(
        "POST",
        f"{QUOTA_URL}/v1/quota/check",
        headers={"X-Ingress-Auth": "wrong", "Content-Type": "application/json"},
        data=b'{"tenant_id":"x","app_id":"y","api_id":"z"}',
        timeout=5,
    )
    print(f"  POST /v1/quota/check (wrong secret) -> {st_bad} {raw_bad!r}")
    assert st_bad == 401, f"R3A-A 错 secret 想要 401，实际 {st_bad}"

    # 对 secret 但 body 缺字段 → 400 表示已过鉴权到 handler
    st_ok, _ = http(
        "POST",
        f"{QUOTA_URL}/v1/quota/check",
        headers=auth_headers({"Content-Type": "application/json"}),
        data=b"{}",
        timeout=5,
    )
    print(f"  POST /v1/quota/check (good secret + empty body) -> {st_ok}")
    assert st_ok in (200, 400), f"R3A-A good secret 想要 200/400，实际 {st_ok}"
    print("  [R3A-A] 鉴权信任入口 OK（live 200 + no/bad secret 401 + good secret 入 handler）")


def assert_check_shape():
    """R3A-B: check 响应字段集合 == Python QuotaCheckResponse keys + seed-driven source.

    Python models.py: QuotaCheckResponse{allowed, tier_blocked, limit, remaining,
    retry_after_seconds, rule_source}。Go 必须返同样 6 字段（不多不少，无
    current/reset_ms），且 tier_blocked/limit/remaining 在 allowed=True 路径下为
    JSON null（pointer types marshal to null）。

    T2 后：rule_source 必须为 "app"（seed app_smoke_r3a.rate_limit 覆盖 api_version），
    不能是 "default"——这证明 Go LoadRules 真的从 PG 加载了 seed（不是 unlimited）。
    remaining 应 = APP_SECOND_MAX - 1（第一次 check，cost=1）。
    """
    expected_keys = {
        "allowed",
        "tier_blocked",
        "limit",
        "remaining",
        "retry_after_seconds",
        "rule_source",
    }
    body = json.dumps(
        {"tenant_id": TENANT_ID, "app_id": APP_ID, "api_id": API_ID, "cost": 1}
    ).encode()
    st, raw = http(
        "POST",
        f"{QUOTA_URL}/v1/quota/check",
        headers=auth_headers({"Content-Type": "application/json"}),
        data=body,
        timeout=10,
    )
    print(f"  POST /v1/quota/check -> HTTP {st} {raw!r}")
    assert st == 200, f"R3A-B HTTP {st} (want 200)"
    payload = json.loads(raw)
    got_keys = set(payload.keys())
    assert got_keys == expected_keys, (
        f"R3A-B 字段集合不匹配：got={sorted(got_keys)} want={sorted(expected_keys)} "
        f"(缺失={expected_keys - got_keys}, 多余={got_keys - expected_keys})"
    )
    # allowed=True 路径下，tier_blocked/limit 必为 null（pointer 不赋值）
    assert payload["allowed"] is True, f"R3A-B 预期默认放行 allowed=True，实际 {payload}"
    assert (
        payload["tier_blocked"] is None
    ), f"R3A-B allowed=True 时 tier_blocked 必须 null，实际 {payload['tier_blocked']!r}"
    assert (
        "current" not in payload and "reset_ms" not in payload
    ), f"R3A-B 旧字段泄漏：current/reset_ms 不应出现，payload={payload}"
    # rule_source VALUE：Python routes.py:58 重写后只可能是
    # "fallback"（Redis 故障）/ "app" / "tenant" / "api_version" / "default"。
    # 不可能出现 "rules" / "unlimited"（limiter 内部标签会被 source 覆盖）。
    valid_sources = {"fallback", "app", "tenant", "api_version", "default"}
    assert payload["rule_source"] in valid_sources, (
        f"R3A-B rule_source 值 {payload['rule_source']!r} 不在 Python 契约集合 {valid_sources}；"
        f"handler.check 是否漏了 routes.py:58-style 重写？"
    )
    # T2: seed app_smoke_r3a.rate_limit 覆盖 → source 必须 "app"（不是 default）
    assert payload["rule_source"] == "app", (
        f"R3A-B rule_source 想要 'app'（seed app override），实际 {payload['rule_source']!r}；"
        f"如为 'default' 检查：13-quota-rules-seed.sql 是否 apply？Go quota AfterConnect "
        f"is_platform_admin 是否生效（RLS 否则隐藏 app 行）？"
    )
    # remaining = APP_SECOND_MAX - cost（首次 check，second slot 之前没用过）
    assert payload["remaining"] == APP_SECOND_MAX - 1, (
        f"R3A-B remaining 想要 {APP_SECOND_MAX - 1}（app second max={APP_SECOND_MAX} - cost 1），"
        f"实际 {payload['remaining']!r}"
    )
    print(
        f"  [R3A-B] check 响应字段 OK —— keys={sorted(got_keys)}, "
        f"allowed={payload['allowed']}, tier_blocked=null, "
        f"rule_source={payload['rule_source']!r}, remaining={payload['remaining']}"
    )
    return payload


def assert_usage_shape():
    """R3A-C: GET /v1/quota/usage 扁平结构 == Python UsageResponse。

    Python models.py: UsageResponse{tenant_id, app_id, api_id, second, minute, day}；
    UsagePoint{window_seconds, used, limit}（limit 可 null）。
    """
    expected_top = {"tenant_id", "app_id", "api_id", "second", "minute", "day"}
    expected_point = {"window_seconds", "used", "limit"}
    st, raw = http(
        "GET",
        f"{QUOTA_URL}/v1/quota/usage" f"?tenant_id={TENANT_ID}&app_id={APP_ID}&api_id={API_ID}",
        headers=auth_headers(),
        timeout=10,
    )
    print(f"  GET /v1/quota/usage -> HTTP {st} {raw!r}")
    assert st == 200, f"R3A-C HTTP {st} (want 200)"
    payload = json.loads(raw)
    got_top = set(payload.keys())
    assert got_top == expected_top, (
        f"R3A-C 顶层字段不匹配：got={sorted(got_top)} want={sorted(expected_top)} "
        f"(缺失/多余：{expected_top ^ got_top})"
    )
    for tier in ("second", "minute", "day"):
        pt = payload[tier]
        got_pt = set(pt.keys())
        assert (
            got_pt == expected_point
        ), f"R3A-C {tier}.point 字段不匹配：got={sorted(got_pt)} want={sorted(expected_point)}"
        # canonical windows（Python _TIER_DEFS）
        expected_win = {"second": 1, "minute": 60, "day": 86400}[tier]
        assert pt["window_seconds"] == expected_win, (
            f"R3A-C {tier}.window_seconds 想要 {expected_win}（Python canonical）, "
            f"实际 {pt['window_seconds']}"
        )
        # 第 0 章 / default rules 下 second/minute/day 都启用了，所以 limit 非 null
    # T2 seed-driven 限值断言：
    #   - second.limit = APP_SECOND_MAX（app override 覆盖 api_version 的 10）
    #   - minute.limit = API_VERSION_MINUTE_MAX（app 未配，api_version 提供）
    #   - day.limit    = API_VERSION_DAY_MAX（app 未配，api_version 提供）
    # 三层 merge 端到端：app 的 second 覆盖生效 + api_version 的 minute/day 透传。
    assert payload["second"]["limit"] == APP_SECOND_MAX, (
        f"R3A-C second.limit 想要 {APP_SECOND_MAX}（app override），"
        f"实际 {payload['second']['limit']!r}"
    )
    assert payload["minute"]["limit"] == API_VERSION_MINUTE_MAX, (
        f"R3A-C minute.limit 想要 {API_VERSION_MINUTE_MAX}（api_version floor），"
        f"实际 {payload['minute']['limit']!r}"
    )
    assert payload["day"]["limit"] == API_VERSION_DAY_MAX, (
        f"R3A-C day.limit 想要 {API_VERSION_DAY_MAX}（api_version floor），"
        f"实际 {payload['day']['limit']!r}"
    )
    # check 后 usage.second.used >= 1（刚 check 过）
    assert (
        payload["second"]["used"] >= 1
    ), f"R3A-C second.used 应 >= 1（刚 check 过），实际 {payload['second']['used']}"
    print(
        f"  [R3A-C] usage 扁平结构 OK —— second={payload['second']}, "
        f"minute={payload['minute']}, day={payload['day']}"
    )


def assert_redis_key():
    """R3A-D: check 后 redis-cli 出现 `t:<tenant>:rate:<api>:<app>:{s|m|d}:<slot>`。

    Python limiter._rate_keys / Go rateKey 都拼成同一格式：
        t:{tenant}:rate:{api}:{app}:{s|m|d}:{slot}
    注意 api 在 app 前（Python 顺序），与 R3a T1 对齐文档一致。
    """
    # 一次 check 触发 3 tier 写入
    body = json.dumps(
        {"tenant_id": TENANT_ID, "app_id": APP_ID, "api_id": API_ID, "cost": 1}
    ).encode()
    st, _ = http(
        "POST",
        f"{QUOTA_URL}/v1/quota/check",
        headers=auth_headers({"Content-Type": "application/json"}),
        data=body,
        timeout=10,
    )
    assert st == 200, f"R3A-D check 想要 200，实际 {st}"

    # scan redis 找匹配 prefix 的 key
    prefix = f"t:{TENANT_ID}:rate:{API_ID}:{APP_ID}:"
    out = sh(
        f"docker exec apihub-redis redis-cli -a apihub_dev_pwd -n 0 "
        f"--scan --pattern '{prefix}*' 2>/dev/null | sort"
    )
    keys = [k for k in out.strip().splitlines() if k]
    print(f"  redis keys under {prefix!r}: {keys}")
    # 期待至少出现 s/m/d 三个 tier 的 key（slot 数值可能跨秒变）
    tiers_found = set()
    for k in keys:
        # t:tenant:rate:api:app:<tier>:<slot> → tier 是倒数第二段
        parts = k.split(":")
        if len(parts) >= 6:
            tiers_found.add(parts[-2])
    assert {"s", "m", "d"}.issubset(
        tiers_found
    ), f"R3A-D redis key 三 tier 未齐全：found={tiers_found} want ⊇ {{s,m,d}}; keys={keys}"
    # 校验每个 key 的形态：tier 后是纯数字 slot
    for k in keys:
        parts = k.split(":")
        slot = parts[-1]
        assert (
            slot.isdigit()
        ), f"R3A-D redis key slot 非数字：{k!r} 末段={slot!r}（应为 unix_ts/window 整数槽）"
    print("  [R3A-D] Redis key 格式 OK —— 三 tier 齐全 (s/m/d)，slot 纯数字")


def assert_lua_atomic():
    """R3A-E: burst N 次并发 check，second tier max=seeded → admitted == seeded max。

    T2 后：规则来自 13-quota-rules-seed.sql 的 tenant_smoke_r3a_lua.rate_limit
    (`{"second":{"max_count":5,...}}`)。LoadRules 走三层 fall-through：
       app(id=app_smoke_r3a AND tenant_id=tenant_smoke_r3a_lua) → 无行（app 在
           tenant_smoke_r3a 名下）→ NULL
       tenant(id=tenant_smoke_r3a_lua) → seeded → second=5
       api_version(api_id=api_smoke_r3a AND tenant_id=tenant_smoke_r3a_lua) →
           无行（api_version 在 tenant_smoke_r3a 名下）→ NULL
    → source="tenant"，second max=5（不是 R3a 历史 hardcoded defaultRules 10）。

    burst 15 个线程同时 check cost=1：Lua 原子保证 second counter 在 5 处闭合，
    admitted==5、blocked==10。5 是非默认值（proves rule loaded from seed JSONB）。
    """
    # 隔离维度：独立 tenant 让 second slot 与主路径不共用
    tenant = LUA_TENANT_ID
    body = json.dumps({"tenant_id": tenant, "app_id": APP_ID, "api_id": API_ID, "cost": 1}).encode()
    headers = auth_headers({"Content-Type": "application/json"})

    # 并发：用线程池（GIL 对 IO-bound urllib 不影响，能制造 Redis 端的并发）
    from concurrent.futures import ThreadPoolExecutor

    results = []
    with ThreadPoolExecutor(max_workers=LUA_BURST_N) as pool:
        futs = [
            pool.submit(http, "POST", f"{QUOTA_URL}/v1/quota/check", headers, body, 10)
            for _ in range(LUA_BURST_N)
        ]
        for f in futs:
            results.append(f.result())
    allowed_count = 0
    blocked_count = 0
    for st, raw in results:
        if st != 200:
            continue
        payload = json.loads(raw)
        if payload.get("allowed"):
            allowed_count += 1
        else:
            blocked_count += 1
    print(
        f"  burst {LUA_BURST_N} 个 check —— HTTP-200 总数={sum(1 for st,_ in results if st==200)}, "
        f"allowed={allowed_count}, blocked={blocked_count}, "
        f"seed tenant_smoke_r3a_lua second-tier max={LUA_TENANT_SECOND_MAX}"
    )
    # seed tenant.rate_limit.second.max_count=5；并发应精确 admitted=5，blocked=10
    # （HTTP 仍是 200，业务层语义 allowed=False 表示被挡 —— 见 Python routes.py 注释）
    assert allowed_count == LUA_TENANT_SECOND_MAX, (
        f"R3A-E Lua 原子性失败：admitted={allowed_count}，期待精确 "
        f"{LUA_TENANT_SECOND_MAX}（seed tenant second max）—— "
        f"多放={allowed_count - LUA_TENANT_SECOND_MAX}"
    )
    assert blocked_count == LUA_BURST_N - LUA_TENANT_SECOND_MAX, (
        f"R3A-E blocked 数不匹配：got={blocked_count}，"
        f"want={LUA_BURST_N - LUA_TENANT_SECOND_MAX}"
    )
    # 抽样一个 blocked 响应验字段 + source
    sample_blocked = next(
        json.loads(raw) for st, raw in results if st == 200 and not json.loads(raw).get("allowed")
    )
    assert (
        sample_blocked["tier_blocked"] == "second"
    ), f"R3A-E blocked.tier_blocked 想要 'second'，实际 {sample_blocked['tier_blocked']!r}"
    assert sample_blocked["limit"] == LUA_TENANT_SECOND_MAX, (
        f"R3A-E blocked.limit 想要 {LUA_TENANT_SECOND_MAX}（seed），"
        f"实际 {sample_blocked['limit']!r}"
    )
    assert (
        sample_blocked["retry_after_seconds"] >= 1
    ), f"R3A-E blocked.retry_after_seconds 想要 >=1，实际 {sample_blocked['retry_after_seconds']!r}"
    # source 必须为 "tenant"（seed 在 tenant 层，app/api_version fall-through）
    assert sample_blocked["rule_source"] == "tenant", (
        f"R3A-E blocked.rule_source 想要 'tenant'（seed tenant_smoke_r3a_lua.rate_limit），"
        f"实际 {sample_blocked['rule_source']!r}"
    )
    print(
        f"  [R3A-E] Lua 原子性 OK —— admitted={allowed_count} (==seed tenant second max), "
        f"blocked={blocked_count}, source={sample_blocked['rule_source']!r}, "
        f"sample blocked={sample_blocked}"
    )


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main():
    probe_env()

    print("== port-forward svc/quota ==")
    open_quota_pf()
    try:
        print("== R3A-A 鉴权信任入口 ==")
        assert_auth()

        print("== R3A-B check 响应字段 ==")
        assert_check_shape()

        print("== R3A-C usage 扁平结构 ==")
        assert_usage_shape()

        print("== R3A-D Redis key 格式 ==")
        assert_redis_key()

        print("== R3A-E Lua 原子性（并发 burst）==")
        assert_lua_atomic()
    finally:
        close_quota_pf()

    print("ALL OK —— R3a Go quota 对照 Python 契约 PASS（auth + check + usage + key + Lua）")
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
