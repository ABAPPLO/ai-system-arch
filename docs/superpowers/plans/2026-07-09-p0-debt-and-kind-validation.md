# P0 技术债清偿 + 现有链路 K8s 验证（kind 全量） Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 清掉 4 项 P0 技术债（trace-svc SQL、依赖锁定、K8s DB 账号 init、CI smoke），并用 kind 真起集群跑通四条核心链路 + APISIX 网关 + trace 查 CH。

**Architecture:** 分 4 阶段递进。Stage 0 纯代码（pytest 验证）；Stage 1 kind 起服务，数据层复用 host docker-compose（prod 本就是托管服务）；Stage 2 port-forward 直打四链路；Stage 3 APISIX（原生 key-auth）进数据面 + trace 端到端查 CH。每阶段独立可验证，撞墙即停于最近成功阶段。

**Tech Stack:** Python 3.11 / FastAPI / asyncpg / ClickHouse / Kafka / Redis；K8s (kind) + Kustomize；Apache APISIX (helm) + etcd；docker-compose 数据层；GitHub Actions CI。

## Global Constraints

- Python 解释器：`/home/applo/.local/bin/python3.11`（系统 python 是 3.8，**不能**跑本项目）。包管理用 `uv`（`/home/applo/.local/bin/uv`）。
- 所有 `pytest` 用 `python3.11 -m pytest`；测试配置 `asyncio_mode=auto`。
- 依赖锁定目标版本（Stage 0b）：`asyncpg==0.30.0`、`aiokafka==0.12.0`、`clickhouse-connect==0.7.7`、`opentelemetry-api/sdk/exporter-otlp==1.36.0`、`opentelemetry-instrumentation-fastapi/httpx/asyncpg/redis==0.61b0`。
- 数据层 host 暴露端口：PG 5432 / Redis 6379 / Kafka 9094 / ClickHouse 8123 / MinIO 9000 / OTel 4317。业务 PG 账号 `apihub_app` / 密码 `apihub_dev_pwd`（与 compose seed 一致）。
- 每个代码任务以 commit 结尾；提交信息用 conventional commits（`fix(trace-svc): ...` 等）。
- **真实 CH `api_call_log` 列**（`scripts/init-clickhouse/01-schema.sql`，所有 trace 查询必须对齐）：
  `ts, tenant_id(String), tenant_type, app_id(String), api_id(String), api_version_id, trace_id, request_id, method, path, status_code, is_success, latency_ms, request_size, response_size, error_code, error_msg, user_agent, client_ip, backend_type, backend_latency_ms, ai_model, ai_streaming, token_prompt, token_completion, token_total, error_stack_ref`。
- auth verify 端点是 **JSON body** `POST /v1/apikey/verify {"api_key": "..."}`（非 header；spec 此处描述有误，以本计划为准）。
- 已确认：dispatcher `event.py` 产出的 Kafka payload key 与 CH source 表**已对齐**（其 docstring 明示），故 Stage 3b 的 drift 修复预期为 no-op，仅需验证。

---

## File Structure

**Stage 0（代码 + K8s + CI）：**
- Modify: `services/services/trace/src/trace_svc/repository.py` — SQL 全量重写对齐精简 schema
- Modify: `services/services/trace/src/trace_svc/models.py` — 修 docstring（字段保留 Optional）
- Modify: `services/services/trace/src/trace_svc/routes.py` — `_row_to_*` 键名映射
- Modify: `services/services/trace/tests/test_repository.py` + `test_routes.py` — 期望对齐真实列
- Modify: `services/libs/apihub-core/pyproject.toml` — 锁定 4 类依赖
- Create: `deploy/k8s/base/shared/db-init/{configmap.yaml,job.yaml,secret.example.yaml}`
- Create: `.github/workflows/smoke-auth.yml`

**Stage 1（kind overlay + 引导）：**
- Create: `deploy/k8s/overlays/kind/{kustomization.yaml, shared-infra.yaml, mock-backend.yaml, patches/}`
- Create: `scripts/kind/{install-tools.sh, bootstrap.sh, gen-envfrom-patches.py}`
- Modify: `docker-compose.dev.yml`（Kafka EXTERNAL advertised-listener，env 注入）

**Stage 2（smoke）：**
- Create: `scripts/smoke/k8s-links.py`

**Stage 3（APISIX + trace 校验）：**
- Create: `scripts/kind/apisix-setup.sh` + `scripts/smoke/k8s-trace.py`

**文档：**
- Modify: `docs/phase2-integration-findings.md`（追加"K8s 联调结果"小节）

---

## Task 1: trace-svc 对齐精简 ClickHouse schema

**Files:**
- Modify: `services/services/trace/src/trace_svc/repository.py`
- Modify: `services/services/trace/src/trace_svc/models.py`
- Modify: `services/services/trace/src/trace_svc/routes.py`
- Test: `services/services/trace/tests/test_repository.py`, `services/services/trace/tests/test_routes.py`

**Interfaces:**
- Consumes: `apihub_core.clickhouse.{query_all, query_one}`（不变）；真实 `api_call_log` 列（见 Global Constraints）
- Produces: repository 返回的行 dict 键名 = 真实列名（`api_id/path/method/status_code/client_ip/error_code/...`）；`_build_where` 的 `tenant_id` 参数为 String；TIMEOUT 过滤改 `error_code LIKE`。

- [ ] **Step 1: 先更新 `test_repository.py` 编码正确期望（TDD red）**

把 `services/services/trace/tests/test_repository.py` 中以下断言/fixture 改为反映真实 schema。逐处替换：

`TestBuildWhere.test_viewer_tenant_forced` —— tenant_id 现在直传 String：
```python
    def test_viewer_tenant_forced(self):
        """普通用户：强制 viewer_tenant_id 过滤（String，原样透传）。"""
        where, params = repo._build_where(CallQuery(), viewer_tenant_id="100")
        assert "tenant_id = %(tenant_id)s" in where
        assert params["tenant_id"] == "100"
```

`test_viewer_tenant_non_digit` —— 非数字 tenant_id 现在原样透传（不再兜底成 0）：
```python
    def test_viewer_tenant_string_passthrough(self):
        """tenant_id 非数字（如 'system'）→ 原样透传 String。"""
        where, params = repo._build_where(CallQuery(), viewer_tenant_id="system")
        assert params["tenant_id"] == "system"
```

`test_status_timeout` —— 无 is_timeout 列，改 error_code LIKE：
```python
    def test_status_timeout(self):
        q = CallQuery(status=CallStatusFilter.TIMEOUT)
        where, params = repo._build_where(q, viewer_tenant_id=None)
        assert "error_code LIKE %(timeout_pat)s" in where
        assert params["timeout_pat"] == "%timeout%"
```

`test_all_filters` —— api_uuid/app_uuid → api_id/app_id：
```python
        assert "api_id = %(api_id)s" in where
        assert "app_id = %(app_id)s" in where
        assert "trace_id = %(trace_id)s" in where
        assert "is_success = 0" in where
```

`TestListCalls.test_returns_rows` 的 fixture 行键改为真实列名：
```python
        fake_ch["rows"] = [
            {
                "trace_id": "t1",
                "api_id": "api_a",
                "path": "/echo",
                "method": "GET",
                "api_version_id": "v1",
                "app_id": "app_x",
                "client_ip": "10.0.0.1",
                "status_code": 200,
                "is_success": 1,
                "latency_ms": 12,
                "error_code": "",
                "error_msg": "",
                "ts": datetime(2026, 7, 1),
            }
        ]
```

`TestGetCall.test_found` 的 fixture 行键改为真实列：
```python
        fake_ch["row"] = {
            "trace_id": "t1",
            "api_id": "api_a",
            "path": "/echo",
            "method": "GET",
            "api_version_id": "v1",
            "app_id": "app_x",
            "client_ip": "10.0.0.1",
            "request_id": "r1",
            "request_size": 100,
            "response_size": 200,
            "status_code": 200,
            "is_success": 1,
            "latency_ms": 5,
            "backend_latency_ms": 4,
            "ai_streaming": 0,
            "token_prompt": 0,
            "token_completion": 0,
            "token_total": 0,
            "ai_model": "",
            "error_code": "",
            "error_msg": "",
            "ts": datetime(2026, 7, 1),
        }
```

`TestGetCall.test_normal_user_tenant_filter` —— tenant_id String：
```python
        assert params["tenant_id"] == "100"
```

`TestStats.test_full_aggregation` —— 改 GROUP BY 嗅探串 + top_apis 行键：
```python
            if "GROUP BY api_id" in sql:
                return [
                    {"api_id": "api_a", "path": "/echo", "n": 500, "success_n": 490}
                ]
            if "GROUP BY toStartOfHour(ts)" in sql:
                return [
                    {"hour": "2026-07-01 00:00:00", "n": 100, "success_n": 95}
                ]
```

- [ ] **Step 2: 更新 `test_routes.py` 的 fixture 行键 + 去掉对已删列的断言**

`services/services/trace/tests/test_routes.py`：把 `_list_row` base dict 改为真实列键：
```python
def _list_row(**overrides):
    base = {
        "trace_id": "t1",
        "api_id": "api_a",
        "path": "/echo",
        "method": "GET",
        "api_version_id": "v1",
        "app_id": "app_x",
        "client_ip": "10.0.0.1",
        "status_code": 200,
        "is_success": 1,
        "latency_ms": 12,
        "error_code": "",
        "error_msg": "",
        "ts": datetime(2026, 7, 1),
    }
    base.update(overrides)
    return base
```
`_detail_row` 改为只含真实列：
```python
def _detail_row(**overrides):
    base = _list_row()
    base.update({
        "request_id": "r1",
        "request_size": 100,
        "response_size": 200,
        "backend_latency_ms": 10,
        "ai_streaming": 0,
        "token_prompt": 0,
        "token_completion": 0,
        "token_total": 0,
        "ai_model": "",
    })
    base.update(overrides)
    return base
```
`TestGetCall.test_admin_gets_detail` —— span_id 列已删，改断言真实存在的字段：
```python
        monkeypatch.setattr(repo_mod, "get_call", _get)
        resp = await client.get("/v1/trace/calls/tr_abc")
        assert resp.status_code == 200
        body = resp.json()
        assert body["trace_id"] == "tr_abc"
        assert body["is_success"] is True
        assert body["backend_latency_ms"] == 10
        assert body["span_id"] is None  # 列已删，恒为 None
```

- [ ] **Step 3: 跑测试确认 red**

Run: `cd /home/applo/project/ai-system-arch && python3.11 -m pytest services/services/trace/tests/ -v`
Expected: 多个 FAIL（`_build_where`/`stats`/`_row_to_*` 仍用旧列名，断言对不上）。

- [ ] **Step 4: 重写 `repository.py` 对齐精简 schema**

把 `services/services/trace/src/trace_svc/repository.py` 的三处列常量 + `_build_where` + `stats` 改为：

`_build_where`（tenant_id 直传 String；TIMEOUT 用 LIKE）：
```python
    if viewer_tenant_id is not None:
        clauses.append("tenant_id = %(tenant_id)s")
        params["tenant_id"] = viewer_tenant_id  # String，原样透传

    if query.api_id:
        clauses.append("api_id = %(api_id)s")
        params["api_id"] = query.api_id
    if query.app_id:
        clauses.append("app_id = %(app_id)s")
        params["app_id"] = query.app_id
    if query.trace_id:
        clauses.append("trace_id = %(trace_id)s")
        params["trace_id"] = query.trace_id
    if query.since:
        clauses.append("ts >= %(since)s")
        params["since"] = query.since.strftime("%Y-%m-%d %H:%M:%S")
    if query.until:
        clauses.append("ts < %(until)s")
        params["until"] = query.until.strftime("%Y-%m-%d %H:%M:%S")

    if query.status == CallStatusFilter.SUCCESS:
        clauses.append("is_success = 1")
    elif query.status == CallStatusFilter.FAILED:
        clauses.append("is_success = 0")
    elif query.status == CallStatusFilter.TIMEOUT:
        # 精简 schema 无 is_timeout 列 → 按 error_code 近似
        clauses.append("error_code LIKE %(timeout_pat)s")
        params["timeout_pat"] = "%timeout%"
```

`_LIST_COLUMNS`：
```python
_LIST_COLUMNS = """
    trace_id, api_id, path, method, api_version_id,
    app_id, client_ip,
    status_code, is_success, latency_ms,
    error_code, error_msg, ts
"""
```

`_DETAIL_COLUMNS`（仅真实存在列）：
```python
_DETAIL_COLUMNS = """
    trace_id, api_id, path, method, api_version_id,
    app_id, client_ip,
    request_id, request_size, response_size,
    status_code, is_success, latency_ms, backend_latency_ms,
    ai_streaming, token_prompt, token_completion, token_total, ai_model,
    error_code, error_msg, ts
"""
```

`stats()` 的 `base_sql` —— `timeout_count` 改用 error_code LIKE：
```python
    base_sql = f"""
        SELECT
            count() AS total,
            countIf(is_success = 1) AS success_count,
            countIf(is_success = 0) AS failed_count,
            countIf(error_code LIKE '%timeout%') AS timeout_count,
            quantile(0.5)(latency_ms) AS p50_latency_ms,
            quantile(0.95)(latency_ms) AS p95_latency_ms,
            quantile(0.99)(latency_ms) AS p99_latency_ms,
            avg(latency_ms) AS avg_latency_ms
        FROM api_call_log
        {where}
    """
```
`top_apis_sql` —— `api_uuid AS api_id, api_path` → `api_id, path`：
```python
    top_apis_sql = f"""
        SELECT
            api_id,
            path,
            count() AS n,
            countIf(is_success = 1) AS success_n
        FROM api_call_log
        {where}
        GROUP BY api_id, path
        ORDER BY n DESC
        LIMIT 10
    """
```
top_apis 行映射 —— `r["api_path"]` → `r["path"]`：
```python
    top_apis = [
        {
            "api_id": r["api_id"],
            "api_path": r["path"],
            "n": int(r["n"]),
            "success_rate": (int(r["success_n"]) / int(r["n"])) if int(r["n"]) else 0.0,
        }
        for r in top_apis_raw
    ]
```
`by_hour_sql` —— `ts_hour`（不存在）→ `toStartOfHour(ts)`：
```python
    by_hour_sql = f"""
        SELECT
            toString(toStartOfHour(ts)) AS hour,
            count() AS n,
            countIf(is_success = 1) AS success_n
        FROM api_call_log
        {where}
        GROUP BY toStartOfHour(ts)
        ORDER BY toStartOfHour(ts) DESC
        LIMIT 168
    """
```

- [ ] **Step 5: 改 `routes.py` 的 `_row_to_*` 键名映射**

`services/services/trace/src/trace_svc/routes.py`，`_row_to_list_item`：
```python
def _row_to_list_item(r: dict[str, Any]) -> CallListItem:
    return CallListItem(
        trace_id=str(r.get("trace_id", "")),
        api_id=str(r.get("api_id", "")),
        api_path=str(r.get("path", "")),
        api_method=str(r.get("method", "GET")),
        api_version=str(r.get("api_version_id", "v1")),
        app_id=str(r.get("app_id", "")),
        app_name=None,  # 列已删，恒 None
        caller_ip=_format_ip(r.get("client_ip")),
        http_status=int(r.get("status_code", 0)),
        is_success=bool(r.get("is_success", 0)),
        is_timeout=False,  # 列已删，恒 False
        latency_ms=int(r.get("latency_ms", 0)),
        error_type=r.get("error_code") or None,
        error_msg=r.get("error_msg") or None,
        ts=r.get("ts"),
    )
```
`_row_to_detail`（仅映射真实列；其余模型字段置 None/默认）：
```python
def _row_to_detail(r: dict[str, Any]) -> CallDetail:
    return CallDetail(
        trace_id=str(r.get("trace_id", "")),
        parent_trace_id=None,
        span_id=None,
        api_id=str(r.get("api_id", "")),
        api_path=str(r.get("path", "")),
        api_method=str(r.get("method", "GET")),
        api_version=str(r.get("api_version_id", "v1")),
        api_mode=None,
        app_id=str(r.get("app_id", "")),
        app_name=None,
        caller_ip=_format_ip(r.get("client_ip")),
        env=None,
        gateway_node=None,
        req_id=r.get("request_id") or None,
        req_size=int(r.get("request_size", 0)) if r.get("request_size") is not None else None,
        resp_size=int(r.get("response_size", 0)) if r.get("response_size") is not None else None,
        gateway_latency_ms=None,
        backend_latency_ms=int(r.get("backend_latency_ms", 0))
        if r.get("backend_latency_ms") is not None
        else None,
        http_status=int(r.get("status_code", 0)),
        is_success=bool(r.get("is_success", 0)),
        is_timeout=False,
        latency_ms=int(r.get("latency_ms", 0)),
        is_streaming=bool(r.get("ai_streaming", 0)),
        token_prompt=int(r.get("token_prompt", 0)) if r.get("token_prompt") is not None else None,
        token_completion=int(r.get("token_completion", 0)) if r.get("token_completion") is not None else None,
        token_total=int(r.get("token_total", 0)) if r.get("token_total") is not None else None,
        ai_model=r.get("ai_model") or None,
        error_type=r.get("error_code") or None,
        error_msg=r.get("error_msg") or None,
        is_retry=False,
        retry_no=None,
        task_id=None,
        ts=r.get("ts"),
    )
```

- [ ] **Step 6: 修 `models.py` docstring**

`services/services/trace/src/trace_svc/models.py` 顶部 docstring 改为：
```python
"""trace-svc 查询模型。

ClickHouse 表 api_call_log 的 tenant_id/api_id/app_id 均为 String；
trace_id/path/method 也是字符串。响应字段全部用 str/bool。
部分字段（span_id/retry_no/...）在精简 schema 中无对应列，恒为 None/默认，
保留在模型里以维持 API 契约向后兼容。
"""
```

- [ ] **Step 7: 跑测试确认 green**

Run: `cd /home/applo/project/ai-system-arch && python3.11 -m pytest services/services/trace/tests/ -v`
Expected: 全部 PASS。

- [ ] **Step 8: 回归 apihub-core + 提交**

Run: `python3.11 -m pytest services/libs/apihub-core/tests/ -v`
Expected: 全 PASS（不动 core，确认无环境回归）。
```bash
git add services/services/trace/
git commit -m "fix(trace-svc): 对齐精简 ClickHouse schema（删 12 个不存在的列 + 改 8 个列名 + tenant_id String + TIMEOUT 用 error_code LIKE）"
```

---

## Task 2: 锁定 apihub-core 关键依赖

**Files:**
- Modify: `services/libs/apihub-core/pyproject.toml`

**Interfaces:**
- Produces: 确定性依赖版本，防 OTel 0.40 类 API 漂移。

- [ ] **Step 1: 改 `pyproject.toml` 的 dependencies**

把 `services/libs/apihub-core/pyproject.toml` 里这 10 行精确 pin（其余保持 `>=`）：
```
  "asyncpg==0.30.0",
  "aiokafka==0.12.0",
  "clickhouse-connect==0.7.7",
  "opentelemetry-api==1.36.0",
  "opentelemetry-sdk==1.36.0",
  "opentelemetry-exporter-otlp==1.36.0",
  "opentelemetry-instrumentation-fastapi==0.61b0",
  "opentelemetry-instrumentation-httpx==0.61b0",
  "opentelemetry-instrumentation-asyncpg==0.61b0",
  "opentelemetry-instrumentation-redis==0.61b0",
```

- [ ] **Step 2: 装到独立 venv 验证可解析**

Run:
```bash
cd /home/applo/project/ai-system-arch
uv venv /tmp/apihub-pin-check --python python3.11
uv pip install --python /tmp/apihub-pin-check/bin/python -e services/libs/apihub-core
```
Expected: 安装成功，无版本冲突。
若某 patch 不可解析 → 降一档（如 `clickhouse-connect==0.7.6`）并在本步注释记录最终版本。

- [ ] **Step 3: 跑 apihub-core 测试**

Run: `python3.11 -m pytest services/libs/apihub-core/tests/ -v`
Expected: 全 PASS。

- [ ] **Step 4: 提交**
```bash
git add services/libs/apihub-core/pyproject.toml
git commit -m "chore(apihub-core): 锁定 OTel 0.61b0 / asyncpg 0.30.0 / aiokafka 0.12.0 / clickhouse-connect 0.7.7"
```

---

## Task 3: K8s DB 账号 init Job

**Files:**
- Create: `deploy/k8s/base/shared/db-init/configmap.yaml`
- Create: `deploy/k8s/base/shared/db-init/job.yaml`
- Create: `deploy/k8s/base/shared/db-init/secret.example.yaml`

**Interfaces:**
- Consumes: `scripts/init-db/00-roles.sql` + `99-grants.sql`（内联进 ConfigMap）
- Produces: prod 托管 PG 的 `apihub_app` 业务账号自动 provisioning 入口。

- [ ] **Step 1: 写 ConfigMap（内联现有 SQL）**

Create `deploy/k8s/base/shared/db-init/configmap.yaml`。先读两个源文件确认内容：
```bash
cat services/../../scripts/init-db/00-roles.sql scripts/init-db/99-grants.sql
```
把它们的完整内容分别嵌进 `00-roles.sql` / `99-grants.sql` 键（用 `|` 块标量，原样复制）：
```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: apihub-db-init-sql
  namespace: apihub-system
  labels:
    app.kubernetes.io/part-of: apihub
data:
  00-roles.sql: |
    # <原样复制 scripts/init-db/00-roles.sql 内容>
  99-grants.sql: |
    # <原样复制 scripts/init-db/99-grants.sql 内容>
```

- [ ] **Step 2: 写 Job**

Create `deploy/k8s/base/shared/db-init/job.yaml`：
```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: apihub-db-init
  namespace: apihub-system
  labels:
    app.kubernetes.io/part-of: apihub
spec:
  backoffLimit: 5
  ttlSecondsAfterFinished: 86400
  template:
    spec:
      restartPolicy: OnFailure
      containers:
        - name: psql-init
          image: postgres:16-alpine
          env:
            - name: PG_SUPER_URL
              valueFrom:
                secretKeyRef:
                  name: apihub-db-superuser
                  key: url
          command: ["/bin/sh", "-c"]
          args:
            - |
              set -e
              for f in /sql/00-roles.sql /sql/99-grants.sql; do
                echo "applying $f"
                psql "$PG_SUPER_URL" -v ON_ERROR_STOP=1 -f "$f"
              done
              echo "db-init done"
          volumeMounts:
            - name: sql
              mountPath: /sql
              readOnly: true
      volumes:
        - name: sql
          configMap:
            name: apihub-db-init-sql
```

- [ ] **Step 3: 写 Secret 模板**

Create `deploy/k8s/base/shared/db-init/secret.example.yaml`：
```yaml
# 复制为 secret.yaml 填真实值；prod 走 SealedSecret / ExternalSecret，不进 Git。
apiVersion: v1
kind: Secret
metadata:
  name: apihub-db-superuser
  namespace: apihub-system
type: Opaque
stringData:
  # postgresql://apihub:<superuser_pwd>@<pg-host>:5432/apihub
  url: "postgresql://apihub:CHANGE_ME@apihub-rds.internal:5432/apihub"
```

- [ ] **Step 4: 渲染校验**

Run（client-side dry-run，无需集群）：
```bash
kubectl apply --dry-run=client -f deploy/k8s/base/shared/db-init/job.yaml
kubectl apply --dry-run=client -f deploy/k8s/base/shared/db-init/configmap.yaml
```
Expected: `configured (dry run)`，无 YAML/语法错。（kubectl 已由 Task 5 装好；若先于 Task 5 执行，临时用 `python3 -c "import yaml; list(yaml.safe_load_all(open(f)))"` 做纯语法校验。）

- [ ] **Step 5: 提交**
```bash
git add deploy/k8s/base/shared/db-init/
git commit -m "feat(k8s): DB 账号 init Job（apihub_app 业务账号自动化 provisioning）"
```

---

## Task 4: CI smoke 回归（auth-svc key verify）

**Files:**
- Create: `.github/workflows/smoke-auth.yml`

**Interfaces:**
- Produces: 防"PG_USER 变 superuser / key hash 占位"类回归的 CI gate。

- [ ] **Step 1: 写 workflow**

Create `.github/workflows/smoke-auth.yml`：
```yaml
name: smoke-auth
on:
  pull_request:
    paths:
      - "services/**"
      - "docker-compose.dev.yml"
      - ".github/workflows/smoke-auth.yml"
  workflow_dispatch:

jobs:
  auth-verify:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install uv
      - name: Start minimal stack
        run: docker compose --env-file .env.dev -f docker-compose.dev.yml up -d postgres redis
      - name: Wait for PG
        run: |
          for i in $(seq 1 30); do
            docker exec apihub-pg pg_isready -U apihub && break
            sleep 2
          done
      - name: Install deps
        env:
          PG_HOST: 127.0.0.1
          PG_USER: apihub_app
          PG_PASSWORD: apihub_dev_pwd
          REDIS_HOST: 127.0.0.1
          ENV: test
        run: |
          uv pip install --system -e services/libs/apihub-core
          uv pip install --system -e services/services/auth
      - name: Verify real seeded key
        env:
          PG_HOST: 127.0.0.1
          PG_USER: apihub_app
          PG_PASSWORD: apihub_dev_pwd
          REDIS_HOST: 127.0.0.1
          ENV: dev
        run: |
          uvicorn auth.main:app --host 127.0.0.1 --port 8002 &
          SVC=$!
          for i in $(seq 1 30); do curl -sf http://127.0.0.1:8002/health/live && break; sleep 1; done
          RESP=$(curl -s -X POST http://127.0.0.1:8002/v1/apikey/verify \
            -H 'Content-Type: application/json' \
            -d '{"api_key":"ak_test_a_demo001"}')
          echo "$RESP"
          echo "$RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d.get('tenant_id'), 'no tenant_id'; print('OK tenant=', d['tenant_id'])"
          kill $SVC
```

- [ ] **Step 2: 本地手动复现该流程（证明可跑）**

Run（复现 workflow 关键步骤）：
```bash
cd /home/applo/project/ai-system-arch
docker compose --env-file .env.dev -f docker-compose.dev.yml up -d postgres redis
for i in $(seq 1 30); do docker exec apihub-pg pg_isready -U apihub && break; sleep 2; done
uv venv /tmp/auth-smoke --python python3.11
PG_HOST=127.0.0.1 PG_USER=apihub_app PG_PASSWORD=apihub_dev_pwd REDIS_HOST=127.0.0.1 ENV=dev \
  uv pip install --python /tmp/auth-smoke/bin/python -e services/libs/apihub-core -e services/services/auth
PG_HOST=127.0.0.1 PG_USER=apihub_app PG_PASSWORD=apihub_dev_pwd REDIS_HOST=127.0.0.1 ENV=dev \
  /tmp/auth-smoke/bin/uvicorn auth.main:app --host 127.0.0.1 --port 8002 &
sleep 4
curl -s -X POST http://127.0.0.1:8002/v1/apikey/verify -H 'Content-Type: application/json' \
  -d '{"api_key":"ak_test_a_demo001"}'
```
Expected: 返回含 `tenant_id` 的 200 JSON。若 seed key 名不同，先 `docker exec apihub-pg psql -U apihub -d apihub -c "select key_id from api_key;"` 确认实际 seed 值并相应更新 workflow 里的 key。本地验证后 kill uvicorn。

- [ ] **Step 3: 提交**
```bash
git add .github/workflows/smoke-auth.yml
git commit -m "ci: auth-svc key verify smoke（防 PG_USER 变 superuser / key hash 占位回归）"
```

---

> **Stage 0 完成。** 此时 P0 全部清偿且经 pytest / 本地复现验证。Stage 1 起进入 K8s 实跑；撞墙即停于此仍有交付。

---

## Task 5: 安装 K8s 工具链

**Files:**
- Create: `scripts/kind/install-tools.sh`

- [ ] **Step 1: 写安装脚本**

Create `scripts/kind/install-tools.sh`：
```bash
#!/usr/bin/env bash
# 装 kind / kubectl / kustomize 到 ~/.local/bin（无 sudo）。已存在则跳过。
set -euo pipefail
BIN="$HOME/.local/bin"
mkdir -p "$BIN"
KIND_VER=v0.24.1
KCTL_VER=v1.31.0
KUST_VER=v5.5.0
ARCH=amd64

if ! command -v kind >/dev/null 2>&1; then
  curl -sSL "https://kind.sigs.k8s.io/dl/${KIND_VER}/kind-linux-${ARCH}" -o "$BIN/kind"
  chmod +x "$BIN/kind"
fi
if ! command -v kubectl >/dev/null 2>&1; then
  curl -sSL "https://dl.k8s.io/release/${KCTL_VER}/bin/linux/${ARCH}/kubectl" -o "$BIN/kubectl"
  chmod +x "$BIN/kubectl"
fi
if ! command -v kustomize >/dev/null 2>&1; then
  curl -sSL "https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize%2F${KUST_VER}/kustomize_${KUST_VER}_linux_${ARCH}.tar.gz" \
    | tar -xz -C "$BIN"
fi
echo "installed:"; kind version; kubectl version --client; kustomize version
```

- [ ] **Step 2: 执行 + 验证**

Run: `cd /home/applo/project/ai-system-arch && bash scripts/kind/install-tools.sh && export PATH="$HOME/.local/bin:$PATH"`
Expected: 打印三个工具版本号，无报错。

- [ ] **Step 3: 提交**
```bash
git add scripts/kind/install-tools.sh
git commit -m "chore(kind): 工具链安装脚本（kind/kubectl/kustomize）"
```

---

## Task 6: kind overlay（infra 重定向 + 共享 secret + mock-backend）

**Files:**
- Create: `deploy/k8s/overlays/kind/kustomization.yaml`
- Create: `deploy/k8s/overlays/kind/shared-infra.yaml`
- Create: `deploy/k8s/overlays/kind/mock-backend.yaml`
- Create: `scripts/kind/gen-envfrom-patches.py`（生成 11 个 envFrom patch）
- Modify: `docker-compose.dev.yml`（Kafka advertised-listener，env 注入）

**Interfaces:**
- Produces: `deploy/k8s/overlays/kind/` —— `kustomize build` 产出可在 kind 跑的全套 manifests，infra 指向 host compose。

- [ ] **Step 1: 写共享 ConfigMap + Secret（占位 host-ip 由 bootstrap 脚本 sed 注入）**

Create `deploy/k8s/overlays/kind/shared-infra.yaml`（`__HOST_IP__` 占位，bootstrap 阶段 sed 替换为探测到的 host 网桥 IP）：
```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: apihub-shared-infra
  namespace: apihub-system
data:
  PG_HOST: "__HOST_IP__"
  PG_PORT: "5432"
  PG_USER: "apihub_app"
  PG_DATABASE: "apihub"
  PG_SSL: "disable"
  REDIS_HOST: "__HOST_IP__"
  REDIS_PORT: "6379"
  CH_HOST: "__HOST_IP__"
  CH_PORT: "8123"
  CH_USERNAME: "apihub"
  KAFKA_BROKERS: "__HOST_IP__:9094"
  OTEL_EXPORTER_OTLP_ENDPOINT: "http://__HOST_IP__:4317"
  ENV: "dev"
---
apiVersion: v1
kind: Secret
metadata:
  name: apihub-shared-secret
  namespace: apihub-system
type: Opaque
stringData:
  PG_PASSWORD: "apihub_dev_pwd"
  REDIS_PASSWORD: "apihub_dev_pwd"
  CH_PASSWORD: "apihub_dev_pwd"
```

- [ ] **Step 2: 写 mock-backend（L1 同步转发目标）**

Create `deploy/k8s/overlays/kind/mock-backend.yaml`（python:3.11-slim 起一个回显 server）：
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: mock-backend
  namespace: apihub-system
spec:
  replicas: 1
  selector: { matchLabels: { app: mock-backend } }
  template:
    metadata: { labels: { app: mock-backend } }
    spec:
      containers:
        - name: echo
          image: python:3.11-slim
          command: ["python3", "-c"]
          args:
            - |
              from http.server import BaseHTTPRequestHandler, HTTPServer
              import json
              class H(BaseHTTPRequestHandler):
                  def do_POST(self):
                      n=int(self.headers.get('Content-Length',0)); b=self.rfile.read(n)
                      self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
                      self.wfile.write(json.dumps({"ok":True,"echo":b.decode()[:200]}).encode())
                  def do_GET(self):
                      self.send_response(200); self.end_headers(); self.wfile.write(b'{"ok":true}')
                  def log_message(self,*a): pass
              HTTPServer(('0.0.0.0',8080),H).serve_forever()
          ports: [{ containerPort: 8080 }]
---
apiVersion: v1
kind: Service
metadata:
  name: mock-backend
  namespace: apihub-system
spec:
  selector: { app: mock-backend }
  ports: [{ name: http, port: 80, targetPort: 8080 }]
```

- [ ] **Step 3: 写 envFrom patch 生成脚本**

Create `scripts/kind/gen-envfrom-patches.py` —— 为每个服务生成 JSON6902 patch，把 `apihub-shared-infra` + `apihub-shared-secret` **追加**到该 Deployment 第一个 container 的 `envFrom` 列表（`/-` add，保留原有 envFrom 且追加在末尾 → 后者覆盖前者 → 覆盖 base configmap 的 prod DNS 值）：
```python
#!/usr/bin/env python3
"""生成 overlays/kind/patches/<svc>-envfrom.yaml（11 个），向 envFrom 追加共享 infra/secret。

用 JSON6902 的 add 到 list 末尾（path 以 /- 结尾），kustomize strategic-merge 会整体替换
list，故这里不用 strategic-merge，而用 patches + JSON patch 确保是 append。
"""
import json, pathlib

SERVICES = ["api-registry","dispatcher","auth","executor","quota","tenant",
            "admin","docs","trace","retry","workflow"]
OUT = pathlib.Path("deploy/k8s/overlays/kind/patches")
OUT.mkdir(parents=True, exist_ok=True)

TPL = """- op: add
  path: /spec/template/spec/containers/0/envFrom/-
  value:
    configMapRef:
      name: apihub-shared-infra
- op: add
  path: /spec/template/spec/containers/0/envFrom/-
  value:
    secretRef:
      name: apihub-shared-secret
"""
# 每个服务一个 patch 文件，target 指定 Deployment 名
TARGET_TPL = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: {svc}
  namespace: apihub-system
"""
for s in SERVICES:
    body = TARGET_TPL.format(svc=s) + "\n" + TPL
    (OUT / f"{s}-envfrom.yaml").write_text(body)
print(f"wrote {len(SERVICES)} patches to {OUT}")
```
> 注：JSON patch `add` 到 `/envFrom/-` 要求该 list 已存在。base deployment 已有 envFrom（见 api-registry/deployment.yaml），故成立。若某服务 base 无 envFrom，对其改用先 `add /envFrom`（空 list）再 append。

- [ ] **Step 4: 写 kustomization**

Create `deploy/k8s/overlays/kind/kustomization.yaml`：
```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

namespace: apihub-system

resources:
  - ../../base/namespaces/namespaces.yaml
  - shared-infra.yaml
  - mock-backend.yaml
  - ../../services/api-registry/configmap.yaml
  - ../../services/api-registry/deployment.yaml
  - ../../services/dispatcher/configmap.yaml
  - ../../services/dispatcher/deployment.yaml
  - ../../services/auth/configmap.yaml
  - ../../services/auth/deployment.yaml
  - ../../services/executor/configmap.yaml
  - ../../services/executor/deployment.yaml
  - ../../services/quota/configmap.yaml
  - ../../services/quota/deployment.yaml
  - ../../services/tenant/configmap.yaml
  - ../../services/tenant/deployment.yaml
  - ../../services/admin/configmap.yaml
  - ../../services/admin/deployment.yaml
  - ../../services/docs/configmap.yaml
  - ../../services/docs/deployment.yaml
  - ../../services/trace/configmap.yaml
  - ../../services/trace/deployment.yaml
  - ../../services/retry/configmap.yaml
  - ../../services/retry/deployment.yaml
  - ../../services/workflow/configmap.yaml
  - ../../services/workflow/deployment.yaml

patches:
  - path: patches/api-registry-envfrom.yaml
    target: { kind: Deployment, name: api-registry }
  - path: patches/dispatcher-envfrom.yaml
    target: { kind: Deployment, name: dispatcher }
  - path: patches/auth-envfrom.yaml
    target: { kind: Deployment, name: auth }
  - path: patches/executor-envfrom.yaml
    target: { kind: Deployment, name: executor }
  - path: patches/quota-envfrom.yaml
    target: { kind: Deployment, name: quota }
  - path: patches/tenant-envfrom.yaml
    target: { kind: Deployment, name: tenant }
  - path: patches/admin-envfrom.yaml
    target: { kind: Deployment, name: admin }
  - path: patches/docs-envfrom.yaml
    target: { kind: Deployment, name: docs }
  - path: patches/trace-envfrom.yaml
    target: { kind: Deployment, name: trace }
  - path: patches/retry-envfrom.yaml
    target: { kind: Deployment, name: retry }
  - path: patches/workflow-envfrom.yaml
    target: { kind: Deployment, name: workflow }
  # 副本统一压 1（kind 资源友好）
  - target: { kind: Deployment }
    patch: |-
      - op: replace
        path: /spec/replicas
        value: 1
```

- [ ] **Step 5: 生成 patches + 渲染校验**

Run:
```bash
cd /home/applo/project/ai-system-arch
export PATH="$HOME/.local/bin:$PATH"
python3 scripts/kind/gen-envfrom-patches.py
sed 's/__HOST_IP__/172.17.0.1/g' deploy/k8s/overlays/kind/shared-infra.yaml > /tmp/shared-infra-test.yaml
mv /tmp/shared-infra-test.yaml deploy/k8s/overlays/kind/shared-infra.yaml
kustomize build deploy/k8s/overlays/kind >/tmp/kind-render.yaml && echo "render OK, $(wc -l </tmp/kind-render.yaml) lines"
grep -c "apihub-shared-infra" /tmp/kind-render.yaml   # 应 >=11
grep -c "172.17.0.1" /tmp/kind-render.yaml            # 应 >=10
git checkout deploy/k8s/overlays/kind/shared-infra.yaml   # 恢复占位
```
Expected: render OK；shared-infra 与 host-ip 各出现 ≥10 次（11 个 Deployment 各一）。若 shared-infra 计数 < 11 → envFrom patch 未生效，回 Step 3 排查（target name 拼写 / patch 文件被 kustomization 引用）。

- [ ] **Step 6: Kafka advertised-listener override**

在 `docker-compose.dev.yml` 的 `kafka.environment` 里，把 `KAFKA_CFG_ADVERTISED_LISTENERS` 的 EXTERNAL 段从 `localhost:9094` 改为可被 kind pod 解析的 host 网桥地址。为避免写死，新增 env `KAFKA_EXTERNAL_HOST`（默认 `localhost`），bootstrap 脚本探测到网桥 IP 后通过 `.env.dev` 覆盖：
```yaml
      KAFKA_CFG_ADVERTISED_LISTENERS: "PLAINTEXT://kafka:9092,EXTERNAL://${KAFKA_EXTERNAL_HOST:-localhost}:9094"
```

- [ ] **Step 7: 提交**
```bash
git add deploy/k8s/overlays/kind/ scripts/kind/gen-envfrom-patches.py docker-compose.dev.yml
git commit -m "feat(k8s): kind overlay（infra 重定向 host compose + 共享 secret + mock-backend + envFrom patch）"
```

---

## Task 7: kind 起服务（build / load / apply / health）

**Files:**
- Create: `scripts/kind/bootstrap.sh`

- [ ] **Step 1: 写 bootstrap 脚本**

Create `scripts/kind/bootstrap.sh`：
```bash
#!/usr/bin/env bash
# 起 kind 集群 + 复用 host compose 数据层 + 构建 11 镜像 + load + apply + 等 ready。
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
cd "$(dirname "$0")/../.."

# 0) 探测 host 网桥 IP（kind pod 经此访问 host compose 服务）
HOST_IP=$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}')
echo "host bridge gateway: $HOST_IP"

# 1) 确保 compose 数据层在跑 + Kafka advertize 指向 host 网桥
grep -q KAFKA_EXTERNAL_HOST .env.dev 2>/dev/null || echo "KAFKA_EXTERNAL_HOST=$HOST_IP" >> .env.dev
docker compose --env-file .env.dev -f docker-compose.dev.yml up -d

# 2) 创建 kind 集群（预留 APISIX NodePort 30080）
cat >/tmp/kind-config.yaml <<EOF
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
    extraPortMappings:
      - containerPort: 30080
        hostPort: 30080
EOF
kind delete cluster --name apihub 2>/dev/null || true
kind create cluster --name apihub --config /tmp/kind-config.yaml
kubectl config use-context kind-apihub

# 3) 注入 host IP 到 overlay
sed -i "s/__HOST_IP__/$HOST_IP/g" deploy/k8s/overlays/kind/shared-infra.yaml

# 4) 构建 11 镜像 + load 进 kind
SVC=(api-registry dispatcher auth executor quota tenant admin docs trace retry workflow)
for s in "${SVC[@]}"; do
  echo "=== build+load $s ==="
  docker build -f services/services/$s/Dockerfile \
    -t registry.apihub.internal/apihub/$s:0.1.0-dev .
  kind load docker-image "registry.apihub.internal/apihub/$s:0.1.0-dev" --name apihub
done

# 5) apply
kustomize build deploy/k8s/overlays/kind | kubectl apply -f -

# 6) 等 ready
kubectl wait --for=condition=ready pods -n apihub-system --all --timeout=300s

# 7) 健康抽检
kubectl -n apihub-system port-forward svc/api-registry 18000:80 &
PFS=$!
sleep 3
curl -sf http://127.0.0.1:18000/health/ready && echo " <- api-registry ready"
kill $PFS 2>/dev/null || true
echo "DONE: kind stack up. host_ip=$HOST_IP"
```

- [ ] **Step 2: 执行 bootstrap**

Run: `cd /home/applo/project/ai-system-arch && bash scripts/kind/bootstrap.sh 2>&1 | tee /tmp/kind-bootstrap.log`
Expected: 末尾打印 `DONE: kind stack up` + api-registry health 200。
失败排查：
- pod CrashLoopBackOff → `kubectl -n apihub-system logs <pod>`；多半是 env/secret 缺失或连不上 host（查 HOST_IP 是否从 pod 可达：`kubectl run tmp --rm -it --restart=Never --image=busybox -- ping -c1 $HOST_IP`）。
- image pull → 确认 `kind load` 成功 + `imagePullPolicy: IfNotPresent`。
- Kafka 连不上 → 确认 `KAFKA_EXTERNAL_HOST` 已生效、broker advertize 是 `$HOST_IP:9094`（`docker exec apihub-kafka kafka-broker-api-versions.sh --bootstrap-server localhost:9094`）。

- [ ] **Step 3: 全 pod 健康确认**

Run: `kubectl -n apihub-system get pods 2>&1 | grep -vE 'Running|Completed|NAME' || echo "ALL RUNNING"`
Expected: `ALL RUNNING`（mock-backend + 11 服务；workflow stub 模式正常）。

- [ ] **Step 4: 提交**
```bash
git add scripts/kind/bootstrap.sh
git commit -m "chore(kind): 集群引导脚本（探测网桥/compose/build/load/apply/health）"
```

---

## Task 8: Stage 2 四链路 smoke（直打服务）

**Files:**
- Create: `scripts/smoke/k8s-links.py`

**Interfaces:**
- Consumes: Task 7 起的 kind 栈；compose seed 数据（api_key `ak_test_a_demo001`、tenant、api 记录）。
- Produces: 4 条链路在 K8s 的端到端断言脚本。

- [ ] **Step 0: 先摸清三处真实契约（implementer 必做）**

读源码确认 dispatcher 同步转发路径、executor/retry 的 Kafka 消息字段，再填 Step 1 脚本里的占位点：
```bash
# dispatcher 同步转发路由 + request 模型
sed -n '1,200p' services/services/dispatcher/src/dispatcher/routes.py
sed -n '1,120p' services/services/dispatcher/src/dispatcher/models.py
# executor 消费的消息契约
sed -n '1,200p' services/services/executor/src/executor/consumer.py
sed -n '1,120p' services/services/executor/src/executor/models.py
# retry 消费 task-failures 的消息契约
sed -n '1,200p' services/services/retry/src/retry_svc/consumer.py
sed -n '1,120p' services/services/retry/src/retry_svc/models.py
# seed 里已有的 api 记录（L1 复用，避免新建）
docker exec apihub-pg psql -U apihub_app -d apihub -c "select id, base_path, path, method from api_record limit 5;"
docker exec apihub-pg psql -U apihub_app -d apihub -c "select id, path, method, backend_url from api_version limit 5;"
```
据输出确定：L1 的真实转发 URL（dispatcher 如何按 api 路由）、`/tmp/task-msg.json` 与 `/tmp/fail-msg.json` 的字段。

- [ ] **Step 1: 写 smoke 脚本**

Create `scripts/smoke/k8s-links.py`（用 `kubectl port-forward` 暴露服务到 localhost）：
```python
#!/usr/bin/env python3.11
"""Stage 2 四链路 smoke：同步转发 / 异步任务 / 失败重试 / admin 聚合。

前置：Task 7 已起 kind 栈。占位 <API_PATH>/<METHOD> 与消息 JSON 由 Step 0 摸清后填入。
"""
import subprocess, time, sys, json, urllib.request, urllib.error

def sh(cmd): return subprocess.run(cmd, shell=True, check=True, text=True, capture_output=True).stdout

def pf(svc, local, remote_port=80):
    p = subprocess.Popen(
        ["kubectl","-n","apihub-system","port-forward",f"svc/{svc}",f"{local}:{remote_port}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3); return p

def post(url, headers=None, data=None):
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body,
        headers={"Content-Type":"application/json", **(headers or {})}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r: return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e: return e.code, e.read().decode()

def get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r: return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e: return e.code, e.read().decode()

ADMIN_KEY = "ak_test_a_demo001"   # 与 seed 一致；Step 0 若发现不同则改这里

# —— 由 Step 0 摸清后填入 ——
DISPATCHER_FORWARD_PATH = "/v1/forward/<填真实 api path>"   # 例：/v1/users
TASK_MSG_JSON = "/tmp/task-msg.json"     # 内容由 executor 契约决定
FAIL_MSG_JSON = "/tmp/fail-msg.json"     # backend 指向死地址 http://127.0.0.1:9/x

def link1_sync():
    p = pf("dispatcher", 18001)
    try:
        st, body = post(f"http://127.0.0.1:18001{DISPATCHER_FORWARD_PATH}",
                        headers={"X-API-Key": ADMIN_KEY}, data={"hello":"world"})
        assert st == 200, f"L1 fail: {st} {body}"
        print("[L1 sync] OK", st)
    finally: p.terminate()

def link2_async():
    sh(f'docker exec apihub-kafka kafka-console-producer.sh --bootstrap-server localhost:9094 '
       f'--topic task-requests < {TASK_MSG_JSON}')
    time.sleep(6)
    out = sh('docker exec apihub-pg psql -U apihub_app -d apihub -t -c '
             '"select count(*) from task_instance where status=\'succeeded\';"')
    assert int((out.strip() or "0")) >= 1, f"L2 fail: no succeeded task_instance ({out!r})"
    print("[L2 async] OK")

def link3_retry():
    sh(f'docker exec apihub-kafka kafka-console-producer.sh --bootstrap-server localhost:9094 '
       f'--topic task-failures < {FAIL_MSG_JSON}')
    time.sleep(25)   # 等指数退避跑完 max_attempts
    out = sh('docker exec apihub-pg psql -U apihub_app -d apihub -t -c '
             '"select status, count(*) from retry_task group by status;"')
    assert "dead" in out, f"L3 fail: {out!r}"
    print("[L3 retry] OK", out.strip())

def link4_admin():
    p = pf("admin", 18006)
    try:
        st, body = get("http://127.0.0.1:18006/v1/admin/dashboard", headers={"X-API-Key": ADMIN_KEY})
        assert st == 200, f"L4 fail: {st} {body}"
        print("[L4 admin] OK", json.dumps(body)[:200])
    finally: p.terminate()

if __name__ == "__main__":
    fails = []
    for fn in (link1_sync, link2_async, link3_retry, link4_admin):
        try: fn()
        except Exception as e:
            fails.append((fn.__name__, str(e))); print("FAIL", fn.__name__, e)
    if fails:
        for n, e in fails: print("  -", n, e)
        sys.exit(1)
    print("ALL 4 LINKS GREEN")
```

- [ ] **Step 2: 补全契约 JSON 并跑**

据 Step 0 输出填好 `DISPATCHER_FORWARD_PATH`、`/tmp/task-msg.json`、`/tmp/fail-msg.json`。若 seed 无合适 api 记录，用 api-registry port-forward + `POST /v1/apis` 创建一条 backend 指向 `http://mock-backend/apihub-system`（注意 cluster DNS）。
Run: `cd /home/applo/project/ai-system-arch && python3.11 scripts/smoke/k8s-links.py`
Expected: `ALL 4 LINKS GREEN`。失败按脚本打印 + `kubectl -n apihub-system logs deploy/<svc>` 定位。

- [ ] **Step 3: 提交**
```bash
git add scripts/smoke/k8s-links.py
git commit -m "test(smoke): K8s 四链路 smoke（同步/异步/重试/admin 聚合）"
```

---

> **Stage 2 完成 = 「现有链路在 K8s 跑通」核心目标达成。** Stage 3 是完整性加分；按 D5，失败不回滚。

---

## Task 9: APISIX 进数据面（helm + key-auth）

**Files:**
- Create: `scripts/kind/apisix-setup.sh`

- [ ] **Step 1: 写 APISIX 安装 + 配置脚本**

Create `scripts/kind/apisix-setup.sh`：
```bash
#!/usr/bin/env bash
# helm 装 APISIX+etcd（NodePort 30080）+ 配 key-auth consumer + route → dispatcher。
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"

# 1) 装 helm
if ! command -v helm >/dev/null 2>&1; then
  curl -sSL https://get.helm.sh/helm-v3.16.0-linux-amd64.tar.gz \
    | tar -xz -C "$HOME/.local/bin" --strip-components=1 linux-amd64/helm
fi
helm repo add apisix https://charts.apiseven.com
helm repo update

# 2) install（NodePort，固定 admin key）
cat >/tmp/apisix-kind-values.yaml <<'EOF'
gateway:
  type: NodePort
apisix:
  admin_key:
    key: "kind-admin-key"
dashboard:
  enabled: true
etcd:
  replicaCount: 1
EOF
helm upgrade --install apisix apisix/apisix -n apihub-ingress --create-namespace \
  -f /tmp/apisix-kind-values.yaml --wait

# 3) 配 consumer + key-auth + route
ADMIN="http://127.0.0.1:30080/apisix/admin"
KEY="kind-admin-key"
curl -s "$ADMIN/consumers/smoke" -H "X-API-KEY: $KEY" -X PUT -d \
  '{"username":"smoke","plugins":{"key-auth":{"key":"ak_test_a_demo001"}}}'
curl -s "$ADMIN/routes/smoke" -H "X-API-KEY: $KEY" -X PUT -d \
  '{"uri":"/smoke/*","upstream":{"type":"roundrobin","nodes":{"dispatcher.apihub-system:80":1}},"plugins":{"key-auth":{}}}'
echo "APISIX configured"
```

- [ ] **Step 2: 执行 + curl 穿透验证**

Run: `cd /home/applo/project/ai-system-arch && bash scripts/kind/apisix-setup.sh`
Then:
```bash
curl -s -o /dev/null -w "%{http_code}\n" -H "X-API-Key: ak_test_a_demo001" http://127.0.0.1:30080/smoke/health/ready
```
Expected: `200`（经 APISIX key-auth → dispatcher → 200）。401 → consumer key 不匹配；502/503 → upstream node 名错或 dispatcher 未就绪（确认 `dispatcher.apihub-system:80` 可解析：`kubectl -n apihub-system get svc dispatcher`）。

- [ ] **Step 3: 提交**
```bash
git add scripts/kind/apisix-setup.sh
git commit -m "feat(kind): APISIX 网关进数据面（NodePort + key-auth + route→dispatcher）"
```

---

## Task 10: trace 端到端查 CH（验证 Task 1 的 SQL 修复）

**Files:**
- Create: `scripts/smoke/k8s-trace.py`

**Interfaces:**
- Consumes: Task 1 修好的 trace-svc；Task 9 APISIX（或退用 Task 8 直打 dispatcher）产生调用。
- Produces: trace-svc `/v1/trace/calls` 在真实 CH 数据上跑通的端到端证明。

- [ ] **Step 1: 写 trace 校验脚本**

Create `scripts/smoke/k8s-trace.py`：
```python
#!/usr/bin/env python3.11
"""Stage 3b：产生调用 → 等 CH 摄取 → 查 trace-svc /v1/trace/calls 断言有行 + 列名正确。"""
import subprocess, time, sys, json, urllib.request

def pf(svc, local):
    p = subprocess.Popen(["kubectl","-n","apihub-system","port-forward",f"svc/{svc}",f"{local}:80"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3); return p

ADMIN_KEY = "ak_test_a_demo001"
FORWARD_PATH = "/v1/forward/<填与 Task 8 一致的真实 path>"   # 由 Task 8 Step 0 摸清

# 1) 产生若干调用（经 dispatcher port-forward；APISIX 可选）
pg = pf("dispatcher", 18001)
try:
    for _ in range(5):
        req = urllib.request.Request(f"http://127.0.0.1:18001{FORWARD_PATH}",
            headers={"X-API-Key": ADMIN_KEY, "Content-Type":"application/json"},
            method="POST", data=b'{"x":1}')
        try: urllib.request.urlopen(req, timeout=10).read()
        except Exception: pass
finally: pg.terminate()

# 2) 等 CH Kafka engine 消费 + MV 转存
print("waiting 15s for CH ingest..."); time.sleep(15)

# 3) 查 trace-svc
pt = pf("trace", 18008)
try:
    req = urllib.request.Request("http://127.0.0.1:18008/v1/trace/calls?limit=10",
                                 headers={"X-API-Key": ADMIN_KEY})
    with urllib.request.urlopen(req, timeout=15) as r:
        rows = json.loads(r.read())
    assert isinstance(rows, list), rows
    assert len(rows) >= 1, "no rows in trace — Kafka→CH 未摄取 或 trace SQL 仍错"
    assert "api_id" in rows[0] and "http_status" in rows[0], rows[0]
    print("[trace] OK rows=", len(rows), "sample=", json.dumps(rows[0])[:200])
finally: pt.terminate()
```

- [ ] **Step 2: 跑 + 三路排查兜底**

Run: `python3.11 scripts/smoke/k8s-trace.py`
Expected: `[trace] OK rows= N`。
失败按以下顺序排查：
1. CH 有无数据：`docker exec apihub-clickhouse clickhouse-client --query "select count() from apihub.api_call_log"` → 0 = 摄取链路问题。
2. Kafka 有无事件：`docker exec apihub-kafka kafka-console-consumer.sh --bootstrap-server localhost:9094 --topic api-call-events --from-beginning --max-messages 3 --timeout-ms 5000` → 无 = dispatcher 没投。
3. CH 有数据但 trace 报错 → Task 1 SQL 残留 bug，回 Task 1 补单测；仍不通则按 D5 兜底：`docker exec apihub-clickhouse clickhouse-client --query "INSERT INTO apihub.api_call_log SELECT now(),'tenant_a','internal','app_trading','api_demo_a','ver1','trc_t1','req1','GET','/t',200,1,10,1,1,'','','curl','toIPv4(\'10.0.0.1\')','http',5,'',0,0,0,0,0"` 造一行再查，验证查询本身正确（隔离摄取问题）。

- [ ] **Step 3: 提交**
```bash
git add scripts/smoke/k8s-trace.py
git commit -m "test(smoke): trace 端到端查 CH（验证精简 schema SQL 修复）"
```

---

## Task 11: 记录 K8s 联调结果到 findings

**Files:**
- Modify: `docs/phase2-integration-findings.md`

- [ ] **Step 1: 追加"K8s 联调结果"小节**

在 `docs/phase2-integration-findings.md` 末尾（Phase 3 优先级建议之后）追加，填入 Task 7–10 的实际运行结果：
```markdown
---

## K8s 联调结果（kind，2026-07-09）

- **数据层**：复用 host docker-compose（PG/Redis/Kafka/CH/MinIO），未进 kind。
- **Stage 1**：<实际：pod 全 Running / 卡住的服务及根因>
- **Stage 2 四链路**：<L1/L2/L3/L4 各 PASS/FAIL + 关键发现>
- **Stage 3 APISIX**：<NodePort 30080 穿透 200 / 或失败根因>
- **Stage 3 trace 查 CH**：<查出 N 行 / 或降级为 INSERT 造数验证>
- **新坑（若有）**：按"症状/根因/修复/验证"补。
```

- [ ] **Step 2: 提交**
```bash
git add docs/phase2-integration-findings.md
git commit -m "docs(phase2): 追加 K8s kind 联调结果"
```

---

## Self-Review（plan 作者自查记录）

- **Spec 覆盖**：Stage 0 四项（0a→Task1, 0b→Task2, 0c→Task3, 0d→Task4）✓；Stage 1→Task5-7 ✓；Stage 2→Task8 ✓；Stage 3a→Task9、3b→Task10 ✓；交付物文档→Task11 ✓。
- **修正 spec 两处**：auth verify 是 JSON body 非 header（Task4）；Kafka→CH 已对齐、D4 预期 no-op（Task10 注释）。
- **类型一致**：trace repository 返回行键名 = 真实列名；routes `_row_to_*` 与之一致；测试 fixture/断言同步。
- **已知留待 implementer 补全**（非占位符，是依赖真实运行时契约的点）：dispatcher `/v1/forward` 的真实 path 与 request body、task-requests/task-failures 消息 JSON —— Task 8 Step 0 已明确要求 implementer 先读对应 routes.py/models.py 再填。
