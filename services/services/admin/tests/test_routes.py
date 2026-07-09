"""HTTP 端点测试 —— httpx ASGITransport 直打 app。"""

from datetime import datetime

from admin import repository as repo_mod

# ---------- 审计 list ----------


class TestListAudit:
    async def test_admin_lists_all(self, client, as_platform_admin, monkeypatch):
        rows = [
            {
                "id": 1,
                "tenant_id": "t1",
                "actor_type": "user",
                "actor_id": "u1",
                "actor_name": "Alice",
                "action": "create_tenant",
                "resource_type": "tenant",
                "resource_id": "t1",
                "resource_name": None,
                "created_at": datetime(2026, 7, 1),
            }
        ]
        captured = {}

        async def _list(query, *, viewer_tenant_id=None, use_admin_session=False):
            captured["use_admin_session"] = use_admin_session
            captured["viewer"] = viewer_tenant_id
            return rows

        monkeypatch.setattr(repo_mod, "list_events", _list)

        resp = await client.get("/v1/admin/audit")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["action"] == "create_tenant"
        assert captured["use_admin_session"] is True
        assert captured["viewer"] is None

    async def test_normal_user_sees_only_own_tenant(self, client, as_normal_user, monkeypatch):
        as_normal_user("u_bob", tenant_id="tenant_a")

        captured = {}

        async def _list(query, *, viewer_tenant_id=None, use_admin_session=False):
            captured["viewer"] = viewer_tenant_id
            captured["use_admin_session"] = use_admin_session
            return []

        monkeypatch.setattr(repo_mod, "list_events", _list)

        resp = await client.get("/v1/admin/audit")
        assert resp.status_code == 200
        # 普通用户强制 viewer_tenant_id
        assert captured["viewer"] == "tenant_a"
        assert captured["use_admin_session"] is False

    async def test_filter_params_parsed(self, client, as_platform_admin, monkeypatch):
        captured = {}

        async def _list(query, *, viewer_tenant_id=None, use_admin_session=False):
            captured["query"] = query
            return []

        monkeypatch.setattr(repo_mod, "list_events", _list)

        resp = await client.get(
            "/v1/admin/audit",
            params={
                "tenant_id": "t1",
                "action": "create_tenant",
                "limit": "10",
                "offset": "5",
                "since": "2026-07-01T00:00:00",
            },
        )
        assert resp.status_code == 200
        q = captured["query"]
        assert q.tenant_id == "t1"
        assert q.action == "create_tenant"
        assert q.limit == 10
        assert q.offset == 5
        assert q.since == datetime(2026, 7, 1)

    async def test_bad_limit_rejected(self, client, as_platform_admin):
        resp = await client.get("/v1/admin/audit", params={"limit": "not-int"})
        assert resp.status_code == 400


# ---------- 审计 detail ----------


class TestGetAudit:
    async def test_admin_gets_detail(self, client, as_platform_admin, monkeypatch):
        async def _get(aid, *, viewer_tenant_id=None, use_admin_session=False):
            assert use_admin_session is True
            return {
                "id": aid,
                "tenant_id": "t1",
                "actor_type": "user",
                "actor_id": "u1",
                "actor_name": "Alice",
                "actor_ip": "10.0.0.1",
                "auth_method": "api_key",
                "action": "create_tenant",
                "resource_type": "tenant",
                "resource_id": "t1",
                "resource_name": None,
                "env": "dev",
                "detail": {"k": "v"},
                "user_agent": "curl",
                "request_id": "r1",
                "trace_id": "tr1",
                "created_at": datetime(2026, 7, 1),
            }

        monkeypatch.setattr(repo_mod, "get_event", _get)
        resp = await client.get("/v1/admin/audit/42")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == 42
        assert body["detail"] == {"k": "v"}

    async def test_normal_user_tenant_scoped(self, client, as_normal_user, monkeypatch):
        as_normal_user("u_bob", tenant_id="t_self")

        captured = {}

        async def _get(aid, *, viewer_tenant_id=None, use_admin_session=False):
            captured["viewer"] = viewer_tenant_id
            captured["use_admin_session"] = use_admin_session
            return {
                "id": aid,
                "tenant_id": "t_self",
                "actor_type": "user",
                "actor_id": "u1",
                "actor_name": "Bob",
                "actor_ip": None,
                "auth_method": "api_key",
                "action": "create_x",
                "resource_type": "x",
                "resource_id": None,
                "resource_name": None,
                "env": None,
                "detail": {},
                "user_agent": None,
                "request_id": None,
                "trace_id": None,
                "created_at": datetime(2026, 7, 1),
            }

        monkeypatch.setattr(repo_mod, "get_event", _get)

        resp = await client.get("/v1/admin/audit/1")
        assert resp.status_code == 200
        assert captured["viewer"] == "t_self"
        assert captured["use_admin_session"] is False


# ---------- 审计 stats ----------


class TestStats:
    async def test_admin_stats(self, client, as_platform_admin, monkeypatch):
        captured = {}

        async def _stats(*, viewer_tenant_id=None, use_admin_session=False, days=7):
            captured["use_admin_session"] = use_admin_session
            captured["days"] = days
            return {
                "total": 100,
                "top_actions": [{"action": "create_tenant", "n": 30}],
                "top_actors": [{"actor_id": "u1", "actor_name": "Alice", "n": 50}],
                "by_day": [{"day": "2026-07-01", "n": 10}],
            }

        monkeypatch.setattr(repo_mod, "stats", _stats)

        resp = await client.get("/v1/admin/audit/stats", params={"days": "30"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 100
        assert len(body["top_actions"]) == 1
        assert captured["days"] == 30

    async def test_days_clamped(self, client, as_platform_admin, monkeypatch):
        captured = {}

        async def _stats(*, viewer_tenant_id=None, use_admin_session=False, days=7):
            captured["days"] = days
            return {"total": 0, "top_actions": [], "top_actors": [], "by_day": []}

        monkeypatch.setattr(repo_mod, "stats", _stats)

        # 超过 90 → clamp 到 90
        await client.get("/v1/admin/audit/stats", params={"days": "200"})
        assert captured["days"] == 90

        # 小于 1 → clamp 到 1
        await client.get("/v1/admin/audit/stats", params={"days": "0"})
        assert captured["days"] == 1


# ---------- 手动 record ----------


class TestRecord:
    async def test_record_single(self, client, monkeypatch):
        """record 端点不强制 admin（内部服务调用）。"""
        captured = {}

        async def _record(entry):
            captured["entry"] = entry
            return 42

        monkeypatch.setattr(repo_mod, "record", _record)

        resp = await client.post(
            "/v1/admin/audit/record",
            json={
                "tenant_id": "t1",
                "action": "custom_action",
                "resource_type": "custom",
            },
        )
        assert resp.status_code == 201
        assert resp.json()["id"] == 42

    async def test_record_failure_returns_zero(self, client, monkeypatch):
        async def _record(entry):
            return 0  # best-effort 失败

        monkeypatch.setattr(repo_mod, "record", _record)
        resp = await client.post(
            "/v1/admin/audit/record",
            json={
                "tenant_id": "t1",
                "action": "x",
                "resource_type": "y",
            },
        )
        assert resp.status_code == 201
        assert resp.json()["recorded"] is False

    async def test_record_batch(self, client, monkeypatch):
        async def _batch(entries):
            return len(entries)

        monkeypatch.setattr(repo_mod, "record_many", _batch)

        resp = await client.post(
            "/v1/admin/audit/record-batch",
            json=[
                {"tenant_id": "t1", "action": "a", "resource_type": "b"},
                {"tenant_id": "t1", "action": "c", "resource_type": "d"},
            ],
        )
        assert resp.status_code == 200
        assert resp.json()["recorded"] is True


# ---------- CSV export (Phase 2) ----------


class TestExportCsv:
    async def test_csv_not_implemented(self, client, as_platform_admin):
        resp = await client.get("/v1/admin/audit/export/csv")
        assert resp.status_code == 501


# ---------- Dashboard ----------


class TestDashboard:
    async def test_dashboard_admin_only(self, client, as_normal_user):
        """普通用户访问 dashboard → 403。"""
        as_normal_user("u_bob", tenant_id="t1")
        resp = await client.get("/v1/admin/dashboard")
        assert resp.status_code == 403

    async def test_dashboard_aggregates(self, client, as_platform_admin, monkeypatch):
        async def _list_tenants(self, *, api_key, parent_id=None):
            return [
                {"id": "t1", "status": "active"},
                {"id": "t2", "status": "suspended"},
                {"id": "t3", "status": "closed"},
                {"id": "t4", "status": "active"},
            ]

        from admin import aggregator as agg_mod

        monkeypatch.setattr(
            agg_mod.get_aggregator().__class__,
            "list_tenants",
            _list_tenants,
        )

        async def _count(query, *, viewer_tenant_id=None, use_admin_session=False):
            return 5

        async def _list_events(query, *, viewer_tenant_id=None, use_admin_session=False):
            return [
                {
                    "id": 1,
                    "tenant_id": "t1",
                    "actor_type": "user",
                    "actor_id": "u1",
                    "actor_name": "Alice",
                    "action": "create_tenant",
                    "resource_type": "tenant",
                    "resource_id": "t1",
                    "resource_name": None,
                    "created_at": datetime(2026, 7, 1),
                }
            ]

        monkeypatch.setattr(repo_mod, "count", _count)
        monkeypatch.setattr(repo_mod, "list_events", _list_events)

        resp = await client.get("/v1/admin/dashboard")
        assert resp.status_code == 200
        body = resp.json()
        assert body["tenants"]["total"] == 4
        assert body["tenants"]["active"] == 2
        assert body["tenants"]["suspended"] == 1
        assert body["tenants"]["closed"] == 1
        assert body["audit_today"] == 5
        assert body["audit_7d"] == 5
        assert len(body["top_recent_events"]) == 1


# ---------- health ----------


class TestHealth:
    async def test_health(self, client):
        resp = await client.get("/v1/admin/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "service": "admin"}


# ---------- 自动审计 middleware ----------


class TestAutoAudit:
    async def test_mutation_triggers_audit(self, client, as_platform_admin, monkeypatch):
        """POST mutation → middleware 调 record_from_request。"""
        called = {}

        async def _record(request, *, status_code=200, extra_detail=None):
            called["status"] = status_code
            called["method"] = request.method
            called["path"] = request.url.path
            return 1

        from admin import routes as routes_mod

        monkeypatch.setattr(routes_mod, "record_from_request", _record)

        # 发起 mutation（list 端点是 GET，但我们调 record 端点是 POST）
        resp = await client.post(
            "/v1/admin/audit/record",
            json={
                "tenant_id": "t1",
                "action": "x",
                "resource_type": "y",
            },
        )
        assert resp.status_code == 201
        # middleware 被调用
        assert called["method"] == "POST"
        assert "/v1/admin/audit/record" in called["path"]

    async def test_get_not_audited(self, client, as_platform_admin, monkeypatch):
        """GET 请求不审计。"""
        called = []

        async def _record(request, *, status_code=200, extra_detail=None):
            called.append(request.url.path)
            return 0

        async def _list(query, *, viewer_tenant_id=None, use_admin_session=False):
            return []

        from admin import routes as routes_mod

        monkeypatch.setattr(routes_mod, "record_from_request", _record)
        monkeypatch.setattr(repo_mod, "list_events", _list)

        await client.get("/v1/admin/audit")
        assert called == []  # 没审计

    async def test_health_not_audited(self, client, monkeypatch):
        called = []

        async def _record(request, *, status_code=200, extra_detail=None):
            called.append(request.url.path)
            return 0

        from admin import routes as routes_mod

        monkeypatch.setattr(routes_mod, "record_from_request", _record)

        # 即使 POST /v1/admin/health（如果有），也不审计
        await client.get("/v1/admin/health")
        assert called == []
