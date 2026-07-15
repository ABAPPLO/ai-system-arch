"""routes 测试 —— 用 httpx ASGITransport 验证端点行为。"""

from datetime import UTC, datetime

import pytest


@pytest.fixture
def stub_repo(monkeypatch):
    """覆盖 repo.* 为可控 spy。"""
    state = {
        "list_failed_return": [],
        "stats_return": {
            "total": 0,
            "pending": 0,
            "running": 0,
            "dead": 0,
            "ignored": 0,
            "succeeded": 0,
            "success_rate": 0.0,
            "by_error_code": {},
        },
        "get_detail_return": None,
        "requeue_return": (True, 42),
        "ignore_return": True,
    }

    async def _list_failed(q):
        return state["list_failed_return"]

    async def _stats():
        return state["stats_return"]

    async def _get_detail(rid):
        return state["get_detail_return"]

    async def _requeue(rid):
        return state["requeue_return"]

    async def _ignore(rid):
        return state["ignore_return"]

    from retry_svc import repository as repo

    monkeypatch.setattr(repo, "list_failed", _list_failed)
    monkeypatch.setattr(repo, "stats", _stats)
    monkeypatch.setattr(repo, "get_retry_task", _get_detail)
    monkeypatch.setattr(repo, "requeue_for_retry", _requeue)
    monkeypatch.setattr(repo, "mark_ignored", _ignore)
    return state


@pytest.fixture
async def client(monkeypatch):
    """httpx ASGITransport client，绕过鉴权。"""
    from apihub_core import auth as auth_mod
    from apihub_core.tenant import (
        TenantContext,
        set_tenant_context,
    )

    ctx = TenantContext(
        tenant_id="42",
        tenant_type="platform",
        app_id="",
        is_platform_admin=True,
    )

    async def _noop_auth(request, settings, api_key):  # noqa: ARG001
        set_tenant_context(ctx)
        return ctx

    monkeypatch.setattr(auth_mod, "authenticate_request", _noop_auth)

    import httpx
    from retry_svc.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"X-API-Key": "ak_test"},
    ) as c:
        yield c


class TestListFailed:
    async def test_returns_list(self, client, stub_repo):
        from retry_svc.models import (
            BackoffPolicy,
            RetryStatus,
            RetryTaskRow,
        )

        stub_repo["list_failed_return"] = [
            RetryTaskRow(
                id=1,
                tenant_id=42,
                trace_id="tr_1",
                api_id=100,
                app_id=200,
                max_attempts=3,
                retry_count=0,
                next_retry_at=None,
                backoff_policy=BackoffPolicy.EXPONENTIAL,
                backoff_base_ms=1000,
                status=RetryStatus.PENDING,
                env="test",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        ]
        resp = await client.get(
            "/v1/retry/failed",
            headers={"X-API-Key": "ak_test"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["id"] == 1
        assert data[0]["status"] == "pending"

    async def test_with_status_filter(self, client, stub_repo):
        # 同样的 stub，验证 status 参数能传过去（不报错）
        resp = await client.get(
            "/v1/retry/failed?status=dead",
            headers={"X-API-Key": "ak_test"},
        )
        assert resp.status_code == 200


class TestStats:
    async def test_returns_stats(self, client, stub_repo):
        stub_repo["stats_return"] = {
            "total": 100,
            "pending": 30,
            "running": 5,
            "dead": 10,
            "ignored": 5,
            "succeeded": 50,
            "success_rate": 0.5,
            "by_error_code": {"backend_http_500": 40},
        }
        resp = await client.get(
            "/v1/retry/stats",
            headers={"X-API-Key": "ak_test"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 100
        assert data["success_rate"] == 0.5
        assert data["by_error_code"]["backend_http_500"] == 40


class TestGetDetail:
    async def test_404_when_not_found(self, client, stub_repo):
        stub_repo["get_detail_return"] = None
        resp = await client.get(
            "/v1/retry/999",
            headers={"X-API-Key": "ak_test"},
        )
        assert resp.status_code == 404

    async def test_returns_detail(self, client, stub_repo):
        from retry_svc.models import (
            BackoffPolicy,
            RetryStatus,
            RetryTaskDetail,
        )

        stub_repo["get_detail_return"] = RetryTaskDetail(
            id=1,
            tenant_id=42,
            trace_id="tr_1",
            api_id=100,
            app_id=200,
            max_attempts=3,
            retry_count=1,
            next_retry_at=None,
            backoff_policy=BackoffPolicy.EXPONENTIAL,
            backoff_base_ms=1000,
            status=RetryStatus.DEAD,
            env="test",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            attempts=[],
            original_request={"task_id": "task_x"},
        )
        resp = await client.get(
            "/v1/retry/1",
            headers={"X-API-Key": "ak_test"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == 1
        assert data["status"] == "dead"
        assert data["original_request"]["task_id"] == "task_x"


class TestTrigger:
    async def test_trigger_success(self, client, stub_repo, monkeypatch):
        stub_repo["requeue_return"] = (True, 42)

        # requeue 成功后 trigger 会 delay_queue.schedule → 真实 Redis；
        # 单测里 stub 成 no-op，行为验证由专门的 re_enqueues 测试覆盖。
        async def _noop_schedule(**kwargs):  # noqa: ARG001
            return None

        monkeypatch.setattr("retry_svc.routes.delay_queue.schedule", _noop_schedule)

        resp = await client.post(
            "/v1/retry/1/trigger",
            headers={"X-API-Key": "ak_test"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "pending"

    async def test_trigger_re_enqueues_to_delay_queue(self, client, monkeypatch):
        """trigger 成功后必须 delay_queue.schedule，否则 worker 取不到。"""
        called = {}

        async def _fake_schedule(*, tenant_id, retry_task_id, next_attempt_at_ts):  # noqa: ARG001
            called["args"] = (tenant_id, retry_task_id)

        monkeypatch.setattr("retry_svc.routes.delay_queue.schedule", _fake_schedule)

        async def _fake_requeue(retry_task_id):
            return (True, "tenant_a")

        monkeypatch.setattr("retry_svc.routes.repo.requeue_for_retry", _fake_requeue)

        resp = await client.post("/v1/retry/42/trigger")
        assert resp.status_code == 200
        assert called.get("args") == ("tenant_a", 42), "trigger 必须 re-enqueue"

    async def test_trigger_404(self, client, stub_repo):
        stub_repo["requeue_return"] = (False, 0)
        resp = await client.post(
            "/v1/retry/999/trigger",
            headers={"X-API-Key": "ak_test"},
        )
        assert resp.status_code == 404


class TestIgnore:
    async def test_ignore_success(self, client, stub_repo):
        stub_repo["ignore_return"] = True
        resp = await client.post(
            "/v1/retry/1/ignore",
            headers={"X-API-Key": "ak_test"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"

    async def test_ignore_404(self, client, stub_repo):
        stub_repo["ignore_return"] = False
        resp = await client.post(
            "/v1/retry/999/ignore",
            headers={"X-API-Key": "ak_test"},
        )
        assert resp.status_code == 404


class TestHealth:
    async def test_health_open(self, client):
        resp = await client.get("/v1/retry/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
