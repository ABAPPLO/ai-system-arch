"""routes 测试 —— HTTP 端点 + 鉴权 + Argo 状态机。"""



class TestHealth:
    async def test_health(self, client):
        resp = await client.get("/v1/workflows/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestSubmitWorkflow:
    async def test_submit_creates_workflow(self, client, stub_repo):
        resp = await client.post(
            "/v1/workflows",
            json={
                "api_id": 100,
                "app_id": 200,
                "trace_id": "tr_abc",
                "spec": {
                    "entrypoint": "main",
                    "templates": [
                        {"name": "main", "script": {"image": "python:3.11"}},
                    ],
                },
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["api_id"] == 100
        assert data["app_id"] == 200
        assert data["trace_id"] == "tr_abc"
        assert data["status"] == "running"
        assert data["argo_name"].startswith("wf-")
        assert "main" in data["spec"]["templates"][0]["name"]

    async def test_submit_argo_failure(self, client, monkeypatch):
        """Argo submit 抛错 → 502。"""
        from workflow_svc import argo_client

        class _Boom:
            async def submit(self, **kwargs):
                raise argo_client.ArgoError("k8s down")

            async def close(self):
                pass

        monkeypatch.setattr(argo_client, "_client", _Boom())

        resp = await client.post(
            "/v1/workflows",
            json={
                "api_id": 1, "app_id": 1, "trace_id": "tr",
                "spec": {"entrypoint": "main"},
            },
        )
        assert resp.status_code == 502


class TestGetWorkflow:
    async def test_404_when_not_found(self, client, stub_repo):
        resp = await client.get("/v1/workflows/999")
        assert resp.status_code == 404

    async def test_returns_detail_with_steps(self, client, stub_repo):
        # 先 submit 一个
        resp = await client.post(
            "/v1/workflows",
            json={
                "api_id": 1, "app_id": 1, "trace_id": "tr_x",
                "spec": {
                    "entrypoint": "main",
                    "templates": [
                        {"name": "main"},
                        {"name": "step2"},
                    ],
                },
            },
        )
        wf_id = resp.json()["id"]

        # 再 GET 详情
        resp = await client.get(f"/v1/workflows/{wf_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == wf_id
        # stub argo 应该返回 2 个 step（main + step2）
        assert len(data["steps"]) == 2
        assert {s["name"] for s in data["steps"]} == {"main", "step2"}


class TestCancel:
    async def test_404_when_not_found(self, client, stub_repo):
        resp = await client.post("/v1/workflows/999/cancel")
        assert resp.status_code == 404

    async def test_cancel_success(self, client, stub_repo):
        resp = await client.post(
            "/v1/workflows",
            json={
                "api_id": 1, "app_id": 1, "trace_id": "tr",
                "spec": {"entrypoint": "main", "templates": [{"name": "main"}]},
            },
        )
        wf_id = resp.json()["id"]

        resp = await client.post(f"/v1/workflows/{wf_id}/cancel")
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"

        # 验证 PG 状态也更新
        from workflow_svc.models import WorkflowStatus
        wf = stub_repo["workflows"][wf_id]
        assert wf.status == WorkflowStatus.CANCELLED
        assert wf.finished_at is not None


class TestResume:
    async def test_resume_after_cancel(self, client, stub_repo):
        resp = await client.post(
            "/v1/workflows",
            json={
                "api_id": 1, "app_id": 1, "trace_id": "tr",
                "spec": {"entrypoint": "main", "templates": [{"name": "main"}]},
            },
        )
        wf_id = resp.json()["id"]

        await client.post(f"/v1/workflows/{wf_id}/cancel")
        resp = await client.post(f"/v1/workflows/{wf_id}/resume")
        assert resp.status_code == 200
        assert resp.json()["status"] == "running"

    async def test_resume_404(self, client, stub_repo):
        resp = await client.post("/v1/workflows/999/resume")
        assert resp.status_code == 404


class TestSteps:
    async def test_get_steps_success(self, client, stub_repo):
        resp = await client.post(
            "/v1/workflows",
            json={
                "api_id": 1, "app_id": 1, "trace_id": "tr",
                "spec": {
                    "entrypoint": "main",
                    "templates": [{"name": "main"}, {"name": "verify"}],
                },
            },
        )
        wf_id = resp.json()["id"]

        resp = await client.get(f"/v1/workflows/{wf_id}/steps")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert {s["name"] for s in data} == {"main", "verify"}

    async def test_get_steps_404(self, client, stub_repo):
        resp = await client.get("/v1/workflows/999/steps")
        assert resp.status_code == 404


class TestLogs:
    async def test_logs_stream_sse(self, client, stub_repo):
        resp = await client.post(
            "/v1/workflows",
            json={
                "api_id": 1, "app_id": 1, "trace_id": "tr",
                "spec": {"entrypoint": "main", "templates": [{"name": "main"}]},
            },
        )
        wf_id = resp.json()["id"]

        resp = await client.get(f"/v1/workflows/{wf_id}/logs")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = resp.text
        assert "data:" in body  # SSE 格式

    async def test_logs_404(self, client, stub_repo):
        resp = await client.get("/v1/workflows/999/logs")
        assert resp.status_code == 404


class TestListWorkflows:
    async def test_empty_list(self, client, stub_repo):
        stub_repo["list_returns"] = []
        resp = await client.get("/v1/workflows")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_returns_list(self, client, stub_repo):
        from datetime import UTC, datetime

        from workflow_svc.models import WorkflowListItem, WorkflowStatus

        stub_repo["list_returns"] = [
            WorkflowListItem(
                id=1, tenant_id=42, workflow_uuid="u1", argo_name="wf-1",
                api_id=100, app_id=200, trace_id="tr1",
                status=WorkflowStatus.RUNNING,
                submitted_at=datetime.now(UTC),
            )
        ]
        resp = await client.get("/v1/workflows?status=running")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["id"] == 1
        assert data[0]["status"] == "running"
