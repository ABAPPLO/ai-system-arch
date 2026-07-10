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
