# Phase 3 P1 验证补齐 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有 kind 集群补齐并端到端验证 Phase 3 三项「事实 P0」：traceparent 贯通（dispatcher→Kafka→executor）、cross-ns DNS 显式断言、workflow stub e2e（dispatcher→workflow-svc 经 APISIX）。

**Architecture:** 复用在线 kind 集群 + host compose 数据层，不重建。三项顺序 A→B→C，每项独立 commit。Task A 实为「补 `_call_backend` 的 W3C traceparent header + 用 Jaeger smoke 首次验证已接通的链」（探查发现 `executor/consumer.py:73` 已包 `consume_with_trace`，故 consumer 侧无需改）。Task C 会顺带修 workflow-svc 的 `api_id/app_id` int-vs-text 潜伏 bug。

**Tech Stack:** Python 3.11 / FastAPI / asyncpg / aiokafka / OpenTelemetry 1.40.0 + instrumentation 0.61b0 / Jaeger 1.57（host :16686）/ APISIX（kind NodePort 30080）/ kind K8s（context `kind-apihub`）。

## Global Constraints

- **分支**：`feat/phase3-p1-validation`（已建，spec 在 `9cd9536`）。off `main`（`0e4e320`）。
- **环境复用**：kind 集群 `kind-apihub`（12 pods Running）+ host docker-compose（PG `apihub-pg`、Redis、Kafka `apihub-kafka`、CH、MinIO、Jaeger `apihub-jaeger` :16686、OTel）。改服务代码须 rebuild 镜像 + `kubectl -n apihub-system rollout restart deploy/<svc>`。
- **账号/密码**：业务账号 `apihub_app` / `apihub_app_dev_pwd`（NOSUPERUSER，走 RLS）。seed：tenant `tenant_a`、app `app_trading`、API key `ak_test_a_demo001`（APISIX key-auth）。APISIX admin key 见 `scripts/smoke/k8s-links.py` 的 `ADMIN_KEY` 常量（沿用）。
- **OTel 版本锁**：不引入新版本；保持 1.40.0 + 0.61b0 配对。
- **RLS 不破坏**：不改 `db_session()`/`SET LOCAL app.tenant_id` 路径；新 SQL 都经 `db_session()`。
- **stub 行为**：`StubArgoClient` submit 后恒 `RUNNING`、无后台推进；steps 由 `_derive_steps` 从 `spec.templates[].name` 派生。断言只能验 `running` + steps 非空。
- **smoke 约定**（仿 `scripts/smoke/k8s-links.py`、`k8s-trace.py`）：常量 `NAMESPACE="apihub-system"`、`APISIX_URL="http://127.0.0.1:30080"`、`DEMO_KEY="ak_test_a_demo001"`；helper `sh()`/`pf()`/`http()`/`psql()`/`kafka_produce()`；退出码 0=OK / 1=assert fail / 2=env unavailable。smoke 在 host 跑（能直连 30080/16686 + kubectl）。
- **每项末尾 commit**；commit message 用 `feat`/`fix`/`test`/`docs` 前缀。

---

## Task 1: traceparent 贯通 —— `_call_backend` 补 W3C header + Jaeger 验证 smoke

> **勘误（相对 spec A 段）**：spec 写「断点在 executor、consumer 未包 consume_with_trace」——**错**。`executor/consumer.py:73` 已 `await core_kafka.consume_with_trace(topic=TOPIC, msg=msg, processor=self._handle)`，链路已接通。本任务只补 `_call_backend` 缺的 W3C `traceparent`（OTel 链在最后 HTTP 跳到 backend 断裂），并用 Jaeger smoke **首次**在 K8s 验证 dispatcher→executor 整条 trace 真连通（task #100 当年没覆盖这条链）。

**Files:**
- Modify: `services/services/executor/src/executor/processor.py:113-138`（`_call_backend` headers）
- Test: `services/services/executor/tests/test_processor.py`（新增 1 个用例）
- Create: `scripts/smoke/k8s-traceparent.py`

**Interfaces:**
- Consumes: `apihub_core.kafka.consume_with_trace`（已接通，不改）；`opentelemetry.propagate.inject`；Jaeger query API `GET http://127.0.0.1:16686/api/traces`。
- Produces: `_call_backend` 现在转发 W3C `traceparent`/`tracestate` 到 backend；`scripts/smoke/k8s-traceparent.py` 作为 traceparent 链路的回归 smoke。

- [ ] **Step 1: 写失败单测 —— `_call_backend` 在活跃 span 下发送 traceparent header**

在 `services/services/executor/tests/test_processor.py` 末尾新增：

```python
async def test_call_backend_forwards_w3c_traceparent(monkeypatch):
    """活跃 span 下，_call_backend 给 backend 的请求头必须含 W3C traceparent。"""
    import httpx
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    # 装一个真 tracer provider，让 propagate.inject 能写出 traceparent
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(trace, "get_tracer", lambda *a, **kw: provider.get_tracer(*a, **kw))

    from executor import processor
    await processor.init_http_client()

    captured: dict = {}

    class _FakeResp:
        status_code = 200
        text = '{"ok": true}'

    async def _fake_post(url, content=None, headers=None, timeout=None):
        captured["headers"] = headers or {}
        return _FakeResp()

    monkeypatch.setattr(processor._client, "post", _fake_post)

    msg = TaskMessage(
        task_id="t1", api_id="a1", api_version_id="v1",
        backend_url="http://mock/echo", payload="", timeout_seconds=5.0,
        tenant_id="tenant_a", app_id="app_trading", request_id="r1", trace_id="abc",
    )

    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("parent-span"):
        result = await processor._call_backend(msg)

    assert result.status == "succeeded"
    assert "traceparent" in captured["headers"], captured["headers"]
    # traceparent 格式：00-<32 hex trace>-<16 hex span>-<2 flags>
    tp = captured["headers"]["traceparent"]
    assert tp.startswith("00-") and len(tp.split("-")) == 4, tp
    await processor.close_http_client()
```

（若 `test_processor.py` 里 `TaskMessage` 未导入，参照文件顶部既有 import 补 `from executor.models import TaskMessage`。）

- [ ] **Step 2: 跑测试确认失败**

```bash
cd services/services/executor && pip install -e '.[test]' 2>/dev/null; \
pytest tests/test_processor.py::test_call_backend_forwards_w3c_traceparent -v
```
Expected: FAIL —— `AssertionError: 'traceparent' not in headers`（当前 `_call_backend` 不发 traceparent）。

- [ ] **Step 3: 实现 —— `_call_backend` 注入 W3C traceparent**

改 `services/services/executor/src/executor/processor.py` 的 `_call_backend`，把现有 headers 构造（113-130 行附近）替换为：

```python
async def _call_backend(msg: TaskMessage) -> TaskResult:
    """POST backend。所有异常都转成 TaskResult，不向上抛。"""
    if _client is None:
        return TaskResult(
            task_id=msg.task_id,
            status="failed",
            error_code="http_client_not_init",
            error_msg="executor http client not initialized",
        )

    started = time.monotonic()
    headers = {
        "Content-Type": "application/json",
        "X-Task-Id": msg.task_id,
        "X-Request-Id": msg.request_id or "",
        "X-Tenant-Id": msg.tenant_id or "",
        "X-Trace-Id": msg.trace_id or "",
    }
    # W3C traceparent：把当前 OTel context（由 consume_with_trace attach）
    # 注入 header，让 OTel 链延续到业务 backend（与既有 X-Trace-Id 共存）。
    from opentelemetry import propagate

    tp: dict[str, str] = {}
    propagate.inject(tp)
    headers.update(tp)

    try:
        resp = await _client.post(
            msg.backend_url,
            content=msg.payload.encode("utf-8") if msg.payload else b"",
            headers=headers,
            timeout=msg.timeout_seconds,
        )
    except httpx.TimeoutException as e:
        # ...（以下保持不变：TimeoutException→timeout / RequestError→failed / 状态码映射）
```

（仅新增 `propagate.inject` 那 4 行 + import；其余 `_call_backend` 体不变。）

- [ ] **Step 4: 跑测试确认通过**

```bash
pytest services/services/executor/tests/test_processor.py::test_call_backend_forwards_w3c_traceparent -v
```
Expected: PASS。顺手跑全文件：`pytest services/services/executor/tests/ -v` 全绿。

- [ ] **Step 5: 写 Jaeger 验证 smoke `scripts/smoke/k8s-traceparent.py`**

完整内容（仿 `k8s-trace.py` 结构：helper + seed async API + 触发 + 查 Jaeger）：

```python
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
EXPORT_WAIT_S = 10


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
    """给 tenant_a seed 一条 async_task API（指向 mock-backend）。"""
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
    open("/tmp/_tp_seed.sql", "w").write(sql)
    sh(f"docker exec -i apihub-pg psql -U apihub_app -d apihub -v ON_ERROR_STOP=1 < /tmp/_tp_seed.sql")


def trigger_async():
    """经 APISIX 触发异步任务，返回 HTTP 状态。"""
    url = f"{APISIX_URL}/dispatch{ASYNC_BASE_PATH}/work"
    st, body = http("POST", url, headers={"X-API-Key": DEMO_KEY, "Content-Type": "application/json"},
                    data=b'{"hello":"trace"}')
    print(f"  trigger POST {url} -> HTTP {st} {body[:120]!r}")
    return st


def find_connected_trace():
    """查 Jaeger，返回是否有一条 trace 同时含 dispatcher SERVER span 与 executor consume span。"""
    url = (f"{JAEGER_URL}/api/traces?service=dispatcher&limit=40&lookback=1h")
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

    print(f"== wait {EXPORT_WAIT_S}s for OTel BatchSpanProcessor export ==")
    time.sleep(EXPORT_WAIT_S)

    print("== query Jaeger for connected trace ==")
    ok, n = find_connected_trace()
    if ok:
        print(f"TRACEPARENT OK —— 找到连通 trace（dispatcher SERVER + executor kafka.consume），共扫 {n} 条")
        sys.exit(0)
    print(f"FAIL: 未找到连通 trace（扫了 {n} 条）—— executor consume span 未链接到 dispatcher trace")
    sys.exit(1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: rebuild executor 镜像 + 重启 + 跑 smoke**

```bash
# rebuild executor（带 _call_backend 改动）并 load 进 kind
make docker-build SERVICE=executor 2>/dev/null || \
  docker build -t apihub/executor:dev -f services/services/executor/Dockerfile services/services/executor
kind load docker-image apihub/executor:dev --name apihub 2>/dev/null || true
kubectl -n apihub-system rollout restart deploy/executor
kubectl -n apihub-system rollout status deploy/executor --timeout=120s
python3 scripts/smoke/k8s-traceparent.py
```
Expected: `TRACEPARENT OK —— 找到连通 trace ...`，退出 0。

> 若 FAIL：先 `kubectl -n apihub-system logs deploy/executor --tail=50` 确认 consumer 在跑、OTel endpoint 可达（`OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector.apihub-monitoring:4317`）；再查 Jaeger UI `http://127.0.0.1:16686` service=executor 是否有 `kafka.consume task-requests` span。

- [ ] **Step 7: commit**

```bash
git add services/services/executor/src/executor/processor.py \
        services/services/executor/tests/test_processor.py \
        scripts/smoke/k8s-traceparent.py
git commit -m "feat(trace): _call_backend 转发 W3C traceparent + Jaeger 连通 trace 回归 smoke

executor/consumer 已包 consume_with_trace（链路已通）；补 _call_backend 缺的 W3C
traceparent 让 OTel 链延续到 backend。新增 k8s-traceparent.py 首次在 K8s 验证
dispatcher→Kafka→executor 为同一条连通 trace。"
```

---

## Task 2: cross-ns DNS —— 显式断言 + findings 记录

**Files:**
- Modify: `scripts/smoke/k8s-links.py`（末尾新增 cross-ns stage）
- Modify: `docs/phase2-integration-findings.md`（Phase 3 P1 该项）

**Interfaces:**
- Consumes: 既有 `k8s-links.py` 的 `http()`/`sh()` helper 与 L1 APISIX→dispatcher 已绿路径。
- Produces: `k8s-links.py` 末尾 `link5_crossns()` stage；findings 里该项标「已验证（当前布局）」。

- [ ] **Step 1: 在 `k8s-links.py` 末尾新增 cross-ns 断言 stage**

在 `k8s-links.py` 的 `main()`（或等价的 stage 编排处）末尾、返回前，追加一个 stage（沿用文件既有的 `http()`、`APISIX_URL`/`DEMO_KEY`/`ADMIN_KEY` 常量与 print 风格）：

```python
def link5_crossns():
    """显式断言跨 namespace 解析：APISIX(apihub-ingress) → dispatcher(apihub-system)。

    当前布局数据层走 host compose（外部），服务全在 apihub-system；唯一真实跨 ns
    调用即 APISIX→dispatcher（已绿）。这里把它从「隐式 200」升为显式断言。
    """
    print("\n== L5 cross-ns (apihub-ingress → apihub-system) ==")
    # 复用 L1 的 smoke-sync echo 路径（APISIX route /dispatch/* → dispatcher.apihub-system:80）
    path = f"/dispatch{SMOKE_BASE_PATH}{SMOKE_API_PATH}"
    st, body = http("POST", f"{APISIX_URL}{path}",
                    headers={"X-API-Key": DEMO_KEY, "Content-Type": "application/json"})
    assert st == 200, f"L5 HTTP {st}: {body}"
    # 断言响应确实来自 dispatcher 链路（mock-backend echo 的 ok 字段）
    assert isinstance(body, dict) and body.get("ok") is True, f"L5 unexpected body: {body}"
    print("  [L5 cross-ns] OK —— APISIX(ingress)→dispatcher(system) 跨 ns 解析成功")
```

并在 `main()` 的 stage 调用序列里加 `link5_crossns()`（紧跟 L4 之后）。

> 若 `main()` 不存在或 stage 编排方式不同，实现者读 `k8s-links.py` 顶部 docstring + 现有 `link1..link4` 调用点，把 `link5_crossns()` 插到相同位置。

- [ ] **Step 2: 跑确认**

```bash
python3 scripts/smoke/k8s-links.py
```
Expected: 末尾出现 `[L5 cross-ns] OK`，整体退出 0。

- [ ] **Step 3: 更新 `docs/phase2-integration-findings.md` Phase 3 P1 该项**

把 P1 第三条（admin dashboard 跨 namespace DNS）改为：

```markdown
- ~~admin dashboard **跨 namespace** DNS~~ → **已验证（当前布局）**：数据层走 host compose（外部 `__HOST_IP__`），业务服务全在 `apihub-system`，服务间无跨 ns 数据调用；唯一真实跨 ns = APISIX(`apihub-ingress`)→dispatcher(`apihub-system`)，`k8s-links.py` L5 显式断言已绿。**待数据服务（PG/Redis/Kafka/CH/MinIO）迁入 `apihub-data` in-cluster 后需重验。**
```

- [ ] **Step 4: commit**

```bash
git add scripts/smoke/k8s-links.py docs/phase2-integration-findings.md
git commit -m "test(k8s): L5 cross-ns 显式断言 APISIX→dispatcher；findings 标已验证"
```

---

## Task 3: workflow —— dispatcher `/v1/jobs` 代理 + settings + 单测

**Files:**
- Modify: `services/libs/apihub-core/src/apihub_core/config.py`（新增 `workflow_service_url`）
- Modify: `deploy/k8s/services/dispatcher/configmap.yaml`（新增 `WORKFLOW_SERVICE_URL`）
- Modify: `deploy/k8s/overlays/kind/`（dispatcher envFrom 补 `WORKFLOW_SERVICE_URL`，若 overlay 显式列 env）
- Modify: `services/services/dispatcher/src/dispatcher/routes.py`（新增 `/v1/jobs` POST + GET；改 501 注释）
- Modify: `services/services/dispatcher/src/dispatcher/main.py`（暴露 workflow httpx client 给 routes）
- Test: `services/services/dispatcher/tests/`（新增 test_jobs.py）

**Interfaces:**
- Consumes: workflow-svc `POST /v1/workflows`（body `SubmitWorkflowRequest`）、`GET /v1/workflows/{id}`；`apihub_core.config.get_settings()`；`opentelemetry.trace`（取 trace_id）。
- Produces: dispatcher `POST /v1/jobs`（→ workflow-svc，201）、`GET /v1/jobs/{job_id}`（→ workflow-svc，200）。

- [ ] **Step 1: settings 加 `workflow_service_url`**

`services/libs/apihub-core/src/apihub_core/config.py`，在 `tenant_service_url`/`executor_service_template` 那一组里加：

```python
    workflow_service_url: str = "http://workflow.apihub-system"
```

- [ ] **Step 2: dispatcher configmap 加 env**

`deploy/k8s/services/dispatcher/configmap.yaml` 的 `data:` 里（`KAFKA_*` 之后）加：

```yaml
  WORKFLOW_SERVICE_URL: http://workflow.apihub-system
```

> kind overlay 若用 envFrom 引用此 configmap（查 `deploy/k8s/overlays/kind/`），无需额外改；若 overlay 显式 patch 了 env 列表，则同步加。实现者先 `grep -n WORKFLOW deploy/k8s/overlays/kind/` 确认。

- [ ] **Step 3: dispatcher main 暴露 workflow client**

`services/services/dispatcher/src/dispatcher/main.py` 的 `_build_routes` 里（已有 `client = httpx.AsyncClient(...)` 与 `set_forwarder(HttpForwarder(client))`），新增一个独立 client 给 workflow 代理（与 forwarder client 隔离，timeout 更短），并注入 routes：

```python
            workflow_client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=10.0),
            )
            app.state.workflow_client = workflow_client
```

并在 `_build_routes` 的 lifespan 关闭处（或 create_app 的 extra_lifespan）补 `await workflow_client.aclose()`。若 main.py 无显式关闭钩子，用 `app.state` + `try/finally` 包住（仿 forwarder client 的现有关闭方式）。

> 实现者读 `main.py` 确认 forwarder client 的创建/关闭结构，把 workflow_client 挂到同处。

- [ ] **Step 4: 写失败单测 `services/services/dispatcher/tests/test_jobs.py`**

```python
"""dispatcher /v1/jobs 代理单测：mock workflow-svc，断言透传与状态码。"""
import pytest

pytestmark = pytest.mark.asyncio


async def test_post_jobs_proxies_to_workflow(async_client, monkeypatch):
    """POST /v1/jobs → workflow-svc POST /v1/workflows，返回 201 + 透传 body。"""
    captured = {}

    class _FakeWF:
        async def post(self, url, json=None, headers=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers or {}
            class _R:
                status_code = 201
                def json(self_inner):
                    return {"id": 42, "status": "running", "argo_name": "wf-x"}
                def raise_for_status(self_inner): pass
            return _R()

    # 把 app.state.workflow_client 换成 fake
    async_client.app.state.workflow_client = _FakeWF()

    resp = await async_client.post(
        "/v1/jobs",
        headers={"X-API-Key": "ak_test_a_demo001"},
        json={"api_id": "smoke-wf-api", "app_id": "app_trading",
              "spec": {"entrypoint": "main", "templates": [{"name": "main"}]}},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["id"] == 42 and body["status"] == "running"
    assert captured["url"].endswith("/v1/workflows"), captured["url"]
    # trace_id 被注入（dispatcher 从 OTel context 取，缺则生成）
    assert "trace_id" in captured["json"]


async def test_get_jobs_proxies_to_workflow(async_client):
    """GET /v1/jobs/{id} → workflow-svc GET /v1/workflows/{id}。"""
    class _FakeWF:
        async def get(self, url, headers=None, timeout=None):
            assert url.endswith("/v1/workflows/42"), url
            class _R:
                status_code = 200
                def json(self_inner):
                    return {"id": 42, "status": "running", "steps": [{"name": "main"}]}
                def raise_for_status(self_inner): pass
            return _R()

    async_client.app.state.workflow_client = _FakeWF()
    resp = await async_client.get("/v1/jobs/42", headers={"X-API-Key": "ak_test_a_demo001"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "running"
```

> `async_client` fixture 若不存在，参照 `services/services/dispatcher/tests/conftest.py` 既有 client fixture（应为 `httpx.ASGITransport(app)` + monkeypatch `authenticate_request` 设 tenant_a）。实现者确认 fixture 名。

- [ ] **Step 5: 跑确认失败**

```bash
pytest services/services/dispatcher/tests/test_jobs.py -v
```
Expected: FAIL —— 404（`/v1/jobs` 路由不存在）。

- [ ] **Step 6: 实现 `/v1/jobs` 路由**

`services/services/dispatcher/src/dispatcher/routes.py`：在 `register_routes` 里新增（保留 `/dispatch/{rest}` 与 health 不变）。501 分支加注释指明 workflow 入口为 `/v1/jobs`：

```python
from fastapi import Request
from fastapi.responses import JSONResponse
from apihub_core.config import get_settings
from opentelemetry import trace
import uuid


def _trace_id() -> str:
    span = trace.get_current_span()
    ctx = span.get_span_context() if span else None
    if ctx and ctx.is_valid:
        return f"{ctx.trace_id:032x}"
    return uuid.uuid4().hex


def _wf_client(request: Request):
    client = getattr(request.app.state, "workflow_client", None)
    if client is None:
        raise ApiError(ErrorCode.INTERNAL, "workflow client not initialized", http_status=500)
    return client
```

在 `register_routes(app)` 内追加：

```python
    @app.post("/v1/jobs", status_code=201)
    async def submit_job(request: Request):
        """workflow 入口（文档 §4）：代理到 workflow-svc POST /v1/workflows。"""
        body = await request.json()
        settings = get_settings()
        wf_body = {
            "api_id": body["api_id"],
            "app_id": body["app_id"],
            "spec": body["spec"],
            "trace_id": body.get("trace_id") or _trace_id(),
            "namespace": body.get("namespace", "apihub-workflow"),
        }
        client = _wf_client(request)
        resp = await client.post(
            f"{settings.workflow_service_url}/v1/workflows",
            json=wf_body,
            headers={"X-API-Key": request.headers.get("X-API-Key", "")},
        )
        if resp.status_code >= 400:
            raise ApiError(ErrorCode.INTERNAL, f"workflow-svc error: {resp.text[:300]}",
                           http_status=502)
        return JSONResponse(status_code=201, content=resp.json())

    @app.get("/v1/jobs/{job_id}")
    async def get_job(job_id: int, request: Request):
        """workflow 轮询：代理到 workflow-svc GET /v1/workflows/{id}。"""
        settings = get_settings()
        client = _wf_client(request)
        resp = await client.get(
            f"{settings.workflow_service_url}/v1/workflows/{job_id}",
            headers={"X-API-Key": request.headers.get("X-API-Key", "")},
        )
        if resp.status_code == 404:
            raise ApiError(ErrorCode.NOT_FOUND, f"job {job_id} not found", http_status=404)
        if resp.status_code >= 400:
            raise ApiError(ErrorCode.INTERNAL, f"workflow-svc error: {resp.text[:300]}",
                           http_status=502)
        return resp.json()
```

并把 `routes.py:53-58` 的 501 分支注释改为：

```python
        if snap.backend_type == "workflow":
            # workflow 走独立入口 POST /v1/jobs（见下方 submit_job），/dispatch 不受理
            raise ApiError(
                ErrorCode.INTERNAL,
                "workflow backend: use POST /v1/jobs (not /dispatch)",
                http_status=501,
            )
```

- [ ] **Step 7: 跑测试通过**

```bash
pytest services/services/dispatcher/tests/test_jobs.py -v
```
Expected: 两个用例 PASS。

- [ ] **Step 8: commit**

```bash
git add services/libs/apihub-core/src/apihub_core/config.py \
        deploy/k8s/services/dispatcher/configmap.yaml \
        deploy/k8s/overlays/kind/ \
        services/services/dispatcher/src/dispatcher/routes.py \
        services/services/dispatcher/src/dispatcher/main.py \
        services/services/dispatcher/tests/test_jobs.py
git commit -m "feat(dispatcher): /v1/jobs 代理 workflow-svc（POST 提交 + GET 轮询，按文档§4）"
```

---

## Task 4: workflow —— APISIX `/v1/jobs` 路由 + e2e smoke（含潜伏 bug 修复）

**Files:**
- Modify: `scripts/kind/apisix-setup.sh`（新增 `/v1/jobs/*` route）
- Create: `scripts/smoke/k8s-workflow.py`
- Likely fix: `services/services/workflow/src/workflow_svc/models.py`（`api_id`/`app_id` int→str）+ `routes.py`/`repository.py` 相应类型

**Interfaces:**
- Consumes: Task 3 的 dispatcher `/v1/jobs`；workflow-svc stub（`argo_mode=stub`）；APISIX admin API（`scripts/kind/apisix-setup.sh` 既有 `${ADMIN}`/`${ADMIN_KEY}`）。
- Produces: `scripts/smoke/k8s-workflow.py` 作为 workflow e2e 回归 smoke。

- [ ] **Step 1: APISIX 加 `/v1/jobs/*` route**

`scripts/kind/apisix-setup.sh`，在既有 `route 'dispatcher'`（`/dispatch/*`）PUT 之后，追加：

```bash
# 6c) route：/v1/jobs/* → dispatcher.apihub-system:80（workflow 入口，key-auth 同 /dispatch/*）
say "upsert route 'jobs' (/v1/jobs/* -> dispatcher.apihub-system:80)"
curl -s "${ADMIN}/routes/jobs" -H "X-API-KEY: ${ADMIN_KEY}" -X PUT \
  -d '{"uri":"/v1/jobs/*","upstream":{"type":"roundrobin","nodes":{"dispatcher.apihub-system:80":1}},"plugins":{"key-auth":{"header":"X-API-Key"}}}' \
  -o /dev/null -w "  route jobs PUT -> %{http_code}\n"
```

手动应用到在线集群（不重建）：

```bash
ADMIN=$(kubectl -n apihub-ingress get svc -l app.kubernetes.io/name=apisix -o jsonpath='{.items[0].spec.clusterIP}') # 或沿用脚本里的 ADMIN 探测
# 直接 source 脚本里的变量后执行上面那段 curl；或重跑 apisix-setup.sh 相关段
```

> 实现者读 `apisix-setup.sh` 顶部的 `ADMIN`/`ADMIN_KEY`/`GATEWAY_NODEPORT` 定义，复用。

- [ ] **Step 2: 写 e2e smoke `scripts/smoke/k8s-workflow.py`**

```python
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
    sql = f"""
    INSERT INTO api (id, tenant_id, name, description, category, base_path, tags, status, visibility)
    VALUES ('{WF_API_ID}', '{TENANT_ID}', 'smoke wf', 'wf e2e', 'smoke',
            '/smoke-wf', ARRAY['smoke'], 'published', 'tenant')
    ON CONFLICT (id) DO NOTHING;
    """
    open("/tmp/_wf_seed.sql", "w").write(sql)
    sh(f"docker exec -i apihub-pg psql -U apihub_app -d apihub -v ON_ERROR_STOP=1 < /tmp/_wf_seed.sql")


def main():
    print("== seed tenant_a workflow api ==")
    seed_wf_api()

    spec = {
        "entrypoint": "main",
        "templates": [
            {"name": "main", "steps": [[{"name": "s1", "template": "echo"}],
                                        [{"name": "s2", "template": "echo"}]]},
            {"name": "echo", "container": {"image": "busybox", "command": ["echo", "hi"]}},
        ],
    }
    body = {"api_id": WF_API_ID, "app_id": "app_trading", "spec": spec}

    print("== POST /v1/jobs via APISIX → dispatcher → workflow-svc ==")
    st, resp = http("POST", f"{APISIX_URL}/v1/jobs",
                    headers={"X-API-Key": DEMO_KEY, "Content-Type": "application/json"},
                    data=json.dumps(body).encode())
    print(f"  POST /v1/jobs -> HTTP {st} {resp[:200]!r}")
    if st == 502 and "verify" in resp.lower():
        print("  [diag] 502 含鉴权错误 —— 多半是 workflow-svc 收到的 X-API-Key 无效或 tenant 不符；查 dispatcher 透传 + workflow-svc tenant middleware")
    assert st == 201, f"POST /v1/jobs HTTP {st}: {resp}"
    wf = json.loads(resp)
    assert "id" in wf and "status" in wf, wf
    wf_id = wf["id"]

    print(f"== GET /v1/jobs/{wf_id} ==")
    st, resp = http("GET", f"{APISIX_URL}/v1/jobs/{wf_id}",
                    headers={"X-API-Key": DEMO_KEY})
    print(f"  GET /v1/jobs/{wf_id} -> HTTP {st} {resp[:200]!r}")
    assert st == 200, f"GET HTTP {st}: {resp}"
    detail = json.loads(resp)
    assert detail.get("status") == "running", detail
    steps = detail.get("steps") or []
    assert len(steps) >= 1, f"steps 空: {detail}"
    print(f"WORKFLOW OK —— workflow_id={wf_id} status=running steps={[s.get('name') for s in steps]}")
    sys.exit(0)


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: rebuild dispatcher + workflow 镜像，重启，首次跑 smoke（预期可能撞 int/text bug）**

```bash
for svc in dispatcher workflow; do
  docker build -t apihub/$svc:dev -f services/services/$svc/Dockerfile services/services/$svc
  kind load docker-image apihub/$svc:dev --name apihub
  kubectl -n apihub-system rollout restart deploy/$svc
  kubectl -n apihub-system rollout status deploy/$svc --timeout=120s
done
python3 scripts/smoke/k8s-workflow.py
```

- [ ] **Step 4: 若撞 workflow-svc int/text 类型 bug，则修（预期会发生）**

症状：POST /v1/jobs 返回 502，workflow-svc 日志报 asyncpg 类型错误（`int` vs `text`），或 pydantic 422（`api_id` 收到 text 但模型声明 int）。

修法：把 workflow-svc 对 `api_id`/`app_id` 的类型从 `int` 改为 `str`（schema 与 seed 都是 text）：

- `services/services/workflow/src/workflow_svc/models.py`：`SubmitWorkflowRequest.api_id: str`、`app_id: str`；`Workflow`/`WorkflowDetail`/`WorkflowListItem` 里对应字段 `int → str`。
- `services/services/workflow/src/workflow_svc/repository.py`：`create_workflow(*, api_id: str, app_id: str, ...)`、`list_workflows` query 的 `api_id`/`app_id` 参数类型同步；`get_workflow` 返回行的 cast 跟随。
- `services/services/workflow/src/workflow_svc/routes.py`：`submit_workflow` 里 `tenant_id = int(ctx.tenant_id)` 保留（tenant_id 是 int），但 `api_id`/`app_id` 直接透传 str。

跑 workflow-svc 既有单测确认不回归：
```bash
pytest services/services/workflow/tests/ -v
```
rebuild workflow 镜像 + 重启，再跑 `python3 scripts/smoke/k8s-workflow.py`，直到退出 0。

> 若 smoke 未撞此 bug（asyncpg 隐式强转通过），跳过本步，但在 commit message 注明「未触发 int/text 修复」。

- [ ] **Step 5: 验收 + commit**

```bash
python3 scripts/smoke/k8s-workflow.py   # 期望: WORKFLOW OK ... 退出 0
git add scripts/kind/apisix-setup.sh scripts/smoke/k8s-workflow.py
# 若做了 Step 4 的类型修复：
git add services/services/workflow/src/workflow_svc/
git commit -m "feat(workflow): APISIX /v1/jobs 路由 + stub e2e smoke（+修 api_id/app_id int→text）"
```

---

## Out of Scope（不在本计划）

- 真装 Argo Workflow CRD + controller，验 `K8sArgoClient`（下轮）。
- MinIO 产物、workflow cancel/resume/logs e2e（下轮）。
- 数据服务迁入 `apihub-data` in-cluster 后的 cross-ns 重验（下轮）。
- Task 1 的 Jaeger smoke 进 CI（依赖 kind 集群，保持手动，与 k8s-links 一致）。

## Self-Review（计划 vs spec 覆盖）

- spec A（traceparent）：Task 1 覆盖（已按勘误收窄：consumer 无需改，只 `_call_backend` header + Jaeger 验证）。✅
- spec B（cross-ns）：Task 2 覆盖（k8s-links L5 + findings）。✅
- spec C（workflow stub e2e）：Task 3（dispatcher /v1/jobs 接线 + 单测）+ Task 4（APISIX route + e2e smoke + 潜伏 bug 修复）。✅
- spec「stub 恒 RUNNING」约束：Task 4 smoke 断言 `running` + steps 非空，不断言 succeeded。✅
- 类型一致：`workflow_service_url`（config）↔ `WORKFLOW_SERVICE_URL`（configmap）↔ `get_settings().workflow_service_url`（routes）一致；`_trace_id()`/`_wf_client()` 定义在 Task 3、Task 3 内使用。✅
