"""dispatcher /v1/jobs 代理单测：mock workflow-svc，断言透传与状态码。"""

import pytest

pytestmark = pytest.mark.asyncio


async def test_post_jobs_proxies_to_workflow(async_client, monkeypatch):
    """POST /v1/jobs → workflow-svc POST /v1/workflows，返回 201 + 透传 body。"""
    captured = {}

    class _FakeWF:
        async def post(self, url, json=None, headers=None, timeout=None):  # noqa: ASYNC109
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers or {}

            class _R:
                status_code = 201

                def json(self_inner):
                    return {"id": 42, "status": "running", "argo_name": "wf-x"}

                def raise_for_status(self_inner):
                    pass

            return _R()

    # 把 app.state.workflow_client 换成 fake
    async_client.app.state.workflow_client = _FakeWF()

    resp = await async_client.post(
        "/v1/jobs",
        headers={"X-API-Key": "ak_test_a_demo001"},
        json={
            "api_id": "smoke-wf-api",
            "app_id": "app_trading",
            "spec": {"entrypoint": "main", "templates": [{"name": "main"}]},
        },
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
        async def get(self, url, headers=None, timeout=None):  # noqa: ASYNC109
            assert url.endswith("/v1/workflows/42"), url

            class _R:
                status_code = 200

                def json(self_inner):
                    return {"id": 42, "status": "running", "steps": [{"name": "main"}]}

                def raise_for_status(self_inner):
                    pass

            return _R()

    async_client.app.state.workflow_client = _FakeWF()
    resp = await async_client.get("/v1/jobs/42", headers={"X-API-Key": "ak_test_a_demo001"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "running"


async def test_post_jobs_missing_fields_returns_422(async_client):
    """缺必填字段 → 422（Pydantic 校验），且不走 workflow-svc。"""

    class _MustNotBeCalledWF:
        async def post(self, *args, **kwargs):  # noqa: ANN003
            raise AssertionError("workflow-svc must not be called on 422")

    async_client.app.state.workflow_client = _MustNotBeCalledWF()
    resp = await async_client.post(
        "/v1/jobs",
        headers={"X-API-Key": "ak_test_a_demo001"},
        json={"app_id": "app_trading"},  # 缺 api_id + spec
    )
    assert resp.status_code == 422, resp.text


async def test_cancel_proxies_to_workflow(async_client):
    """POST /v1/jobs/{id}/cancel → workflow-svc POST /v1/workflows/{id}/cancel。"""
    captured = {}

    class _FakeWF:
        async def post(self, url, json=None, headers=None, timeout=None):  # noqa: ASYNC109
            captured["url"] = url
            captured["headers"] = headers or {}

            class _R:
                status_code = 200

                def json(self_inner):
                    return {"workflow_id": 42, "status": "cancelled"}

                def raise_for_status(self_inner):
                    pass

            return _R()

    async_client.app.state.workflow_client = _FakeWF()
    resp = await async_client.post("/v1/jobs/42/cancel", headers={"X-API-Key": "ak_test_a_demo001"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"
    assert captured["url"].endswith("/v1/workflows/42/cancel"), captured["url"]
    assert captured["headers"]["X-API-Key"] == "ak_test_a_demo001"


async def test_resume_proxies_to_workflow(async_client):
    """POST /v1/jobs/{id}/resume → workflow-svc POST /v1/workflows/{id}/resume。"""

    class _FakeWF:
        async def post(self, url, json=None, headers=None, timeout=None):  # noqa: ASYNC109
            assert url.endswith("/v1/workflows/42/resume"), url

            class _R:
                status_code = 200

                def json(self_inner):
                    return {"workflow_id": 42, "status": "running"}

                def raise_for_status(self_inner):
                    pass

            return _R()

    async_client.app.state.workflow_client = _FakeWF()
    resp = await async_client.post("/v1/jobs/42/resume", headers={"X-API-Key": "ak_test_a_demo001"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "running"


async def test_logs_proxies_sse(async_client):
    """GET /v1/jobs/{id}/logs → workflow-svc GET /v1/workflows/{id}/logs，SSE body 透传。"""
    captured = {}

    class _FakeWF:
        async def get(self, url, headers=None, params=None, timeout=None):  # noqa: ASYNC109
            captured["url"] = url
            captured["params"] = params

            class _R:
                status_code = 200
                content = b'data: {"line":"hi from argo"}\n\n'

                def raise_for_status(self_inner):
                    pass

            return _R()

    async_client.app.state.workflow_client = _FakeWF()
    resp = await async_client.get(
        "/v1/jobs/42/logs?step_name=s1", headers={"X-API-Key": "ak_test_a_demo001"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert b"hi from argo" in resp.content
    assert captured["url"].endswith("/v1/workflows/42/logs"), captured["url"]
    assert captured["params"] == {"step_name": "s1"}
