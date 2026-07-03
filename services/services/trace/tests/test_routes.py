"""HTTP 端点测试 —— httpx ASGITransport 直打 app。"""

from datetime import datetime

import pytest
from trace_svc import repository as repo_mod


def _list_row(**overrides):
    base = {
        "trace_id": "t1",
        "api_uuid": "api_a",
        "api_path": "/echo",
        "api_method": "GET",
        "api_version": "v1",
        "app_uuid": "app_x",
        "app_name": "myapp",
        "caller_ip": "10.0.0.1",
        "http_status": 200,
        "is_success": 1,
        "is_timeout": 0,
        "latency_ms": 12,
        "error_type": "",
        "error_msg": "",
        "ts": datetime(2026, 7, 1),
    }
    base.update(overrides)
    return base


def _detail_row(**overrides):
    base = _list_row()
    base.update(
        {
            "parent_trace_id": "",
            "span_id": "s1",
            "api_mode": "sync",
            "env": "dev",
            "gateway_node": "node-1",
            "req_id": "r1",
            "req_size": 100,
            "resp_size": 200,
            "gateway_latency_ms": 2,
            "backend_latency_ms": 10,
            "is_streaming": 0,
            "token_prompt": 0,
            "token_completion": 0,
            "token_total": 0,
            "ai_model": "",
            "is_retry": 0,
            "retry_no": 0,
            "task_id": "",
        }
    )
    base.update(overrides)
    return base


class TestListCalls:
    async def test_admin_lists_all(self, client, monkeypatch):
        captured = {}

        async def _list(query, *, viewer_tenant_id=None, use_admin_session=False):
            captured["use_admin_session"] = use_admin_session
            captured["viewer"] = viewer_tenant_id
            return [_list_row()]

        monkeypatch.setattr(repo_mod, "list_calls", _list)

        resp = await client.get("/v1/trace/calls")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["trace_id"] == "t1"
        assert body[0]["is_success"] is True
        assert captured["use_admin_session"] is True
        assert captured["viewer"] is None

    async def test_normal_user_tenant_scoped(self, client, as_normal_user, monkeypatch):
        as_normal_user("t_self")

        captured = {}

        async def _list(query, *, viewer_tenant_id=None, use_admin_session=False):
            captured["viewer"] = viewer_tenant_id
            captured["use_admin_session"] = use_admin_session
            return []

        monkeypatch.setattr(repo_mod, "list_calls", _list)

        resp = await client.get("/v1/trace/calls")
        assert resp.status_code == 200
        assert captured["viewer"] == "t_self"
        assert captured["use_admin_session"] is False

    async def test_filter_params_parsed(self, client, monkeypatch):
        captured = {}

        async def _list(query, *, viewer_tenant_id=None, use_admin_session=False):
            captured["query"] = query
            return []

        monkeypatch.setattr(repo_mod, "list_calls", _list)

        resp = await client.get(
            "/v1/trace/calls",
            params={
                "api_id": "api_x",
                "status": "failed",
                "since": "2026-07-01T00:00:00",
                "until": "2026-07-02T00:00:00",
                "limit": "10",
                "offset": "5",
            },
        )
        assert resp.status_code == 200
        q = captured["query"]
        assert q.api_id == "api_x"
        assert q.status.value == "failed"
        assert q.limit == 10
        assert q.offset == 5

    async def test_bad_limit_rejected(self, client):
        resp = await client.get("/v1/trace/calls", params={"limit": "not-int"})
        assert resp.status_code == 422


class TestGetCall:
    async def test_admin_gets_detail(self, client, monkeypatch):
        async def _get(trace_id, *, viewer_tenant_id=None, use_admin_session=False):
            assert use_admin_session is True
            return _detail_row(trace_id=trace_id)

        monkeypatch.setattr(repo_mod, "get_call", _get)
        resp = await client.get("/v1/trace/calls/tr_abc")
        assert resp.status_code == 200
        body = resp.json()
        assert body["trace_id"] == "tr_abc"
        assert body["is_success"] is True
        assert body["span_id"] == "s1"

    async def test_normal_user_tenant_filter(self, client, as_normal_user, monkeypatch):
        as_normal_user("t_self")

        captured = {}

        async def _get(trace_id, *, viewer_tenant_id=None, use_admin_session=False):
            captured["viewer"] = viewer_tenant_id
            captured["use_admin_session"] = use_admin_session
            return _detail_row()

        monkeypatch.setattr(repo_mod, "get_call", _get)

        resp = await client.get("/v1/trace/calls/t1")
        assert resp.status_code == 200
        assert captured["viewer"] == "t_self"
        assert captured["use_admin_session"] is False

    async def test_not_found(self, client, monkeypatch):
        from apihub_core.errors import ApiError, ErrorCode

        async def _raise(trace_id, *, viewer_tenant_id=None, use_admin_session=False):
            raise ApiError(ErrorCode.NOT_FOUND, "not found")

        monkeypatch.setattr(repo_mod, "get_call", _raise)
        resp = await client.get("/v1/trace/calls/missing")
        assert resp.status_code == 404


class TestStats:
    async def test_admin_stats(self, client, monkeypatch):
        async def _stats(query, *, viewer_tenant_id=None, use_admin_session=False):
            return {
                "total": 1000,
                "success_count": 950,
                "failed_count": 50,
                "timeout_count": 10,
                "success_rate": 0.95,
                "p50_latency_ms": 10.0,
                "p95_latency_ms": 100.0,
                "p99_latency_ms": 500.0,
                "avg_latency_ms": 25.0,
                "qps": 1.0,
                "top_apis": [{"api_id": "api_a", "api_path": "/echo", "n": 500, "success_rate": 0.98}],
                "by_hour": [{"hour": "2026-07-01 00:00:00", "n": 100, "success_rate": 0.95}],
            }

        monkeypatch.setattr(repo_mod, "stats", _stats)

        resp = await client.get("/v1/trace/calls/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1000
        assert body["success_rate"] == pytest.approx(0.95)
        assert len(body["top_apis"]) == 1
        assert len(body["by_hour"]) == 1


class TestExport:
    async def test_csv_not_implemented(self, client):
        resp = await client.get("/v1/trace/calls/export")
        assert resp.status_code == 501


class TestHealth:
    async def test_health(self, client):
        resp = await client.get("/v1/trace/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "service": "trace"}
