"""HTTP 端点测试 —— httpx ASGITransport 直打 app。

mock 策略：DB 层（repository.load_rules）+ Redis 层（fakeredis）+ Kafka emit。
覆盖 check / check-strict / refund / usage / billing / health 全分支。
"""

import pytest
from apihub_core import auth as core_auth
from apihub_core.tenant import TenantContext, set_tenant_context
from httpx import ASGITransport, AsyncClient
from quota import repository as repo_mod
from quota import routes as routes_mod
from quota.main import app
from quota.models import LimitRule, QuotaRules


@pytest.fixture
def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def stub_rules(monkeypatch):
    """让 load_rules 返回固定规则，不打 PG。"""
    fixed = QuotaRules(
        second=LimitRule(window_seconds=1, max_count=2),
        minute=LimitRule(window_seconds=60, max_count=10),
        day=LimitRule(window_seconds=86400, max_count=1000),
    )

    async def _stub(tenant_id, app_id, api_id):
        return fixed, "test"

    monkeypatch.setattr(repo_mod, "load_rules", _stub)
    routes_mod.repository.load_rules = _stub
    return fixed


@pytest.fixture
def stub_emit(monkeypatch):
    """收集所有 kafka.emit 调用，不真投 Kafka。"""
    emitted = []

    async def _stub_emit(topic, payload, key=None, extra_headers=None):
        emitted.append((topic, payload))

    from quota import routes as r

    monkeypatch.setattr(r.kafka, "emit", _stub_emit)
    return emitted


@pytest.fixture
def authed(monkeypatch):
    """让 middleware 注入 t1/app_x 上下文。"""

    async def _fake(request, settings, api_key, required_scopes=None):
        ctx = TenantContext(
            tenant_id="t1",
            tenant_type="internal",
            app_id="app_x",
        )
        set_tenant_context(ctx)
        return ctx

    monkeypatch.setattr(core_auth, "authenticate_request", _fake)


# ========== /v1/quota/check ==========


class TestCheck:
    async def test_allows_under_limit(self, client, fake_redis, stub_rules, stub_emit):
        resp = await client.post(
            "/v1/quota/check",
            json={"tenant_id": "t1", "app_id": "app_x", "api_id": "api_users"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["allowed"] is True
        # 推了 Kafka 事件
        assert len(stub_emit) == 1
        assert stub_emit[0][0] == "api-call-events"

    async def test_blocks_when_second_exceeded(self, client, fake_redis, stub_rules, stub_emit):
        """second max=2，连发 3 次第 3 次挡。"""
        for _ in range(2):
            r = await client.post(
                "/v1/quota/check",
                json={"tenant_id": "t1", "app_id": "app_x", "api_id": "api_users"},
            )
            assert r.json()["allowed"] is True

        r3 = await client.post(
            "/v1/quota/check",
            json={"tenant_id": "t1", "app_id": "app_x", "api_id": "api_users"},
        )
        body = r3.json()
        assert body["allowed"] is False
        assert body["tier_blocked"] == "second"
        assert body["limit"] == 2

    async def test_cost_param(self, client, fake_redis, stub_rules):
        """cost=2 一次扣 2，max=2 → 第二次挡。"""
        r1 = await client.post(
            "/v1/quota/check",
            json={
                "tenant_id": "t1",
                "app_id": "app_x",
                "api_id": "api_users",
                "cost": 2,
            },
        )
        assert r1.json()["allowed"] is True

        r2 = await client.post(
            "/v1/quota/check",
            json={"tenant_id": "t1", "app_id": "app_x", "api_id": "api_users"},
        )
        assert r2.json()["allowed"] is False


class TestCheckStrict:
    async def test_strict_returns_429_when_blocked(self, client, fake_redis, stub_rules):
        """check-strict 超了直接抛 429。"""
        # 把 second 打满（max=2）
        for _ in range(2):
            await client.post(
                "/v1/quota/check-strict",
                json={"tenant_id": "t1", "app_id": "app_x", "api_id": "api_users"},
            )

        # 第 3 次应该返回 429
        r = await client.post(
            "/v1/quota/check-strict",
            json={"tenant_id": "t1", "app_id": "app_x", "api_id": "api_users"},
        )
        assert r.status_code == 429
        body = r.json()
        assert body["code"] == 10005  # RATE_LIMITED

    async def test_strict_day_quota_returns_different_code(self, client, fake_redis, monkeypatch):
        """日配额超 → TENANT_QUOTA_EXCEEDED (20004) 而非 RATE_LIMITED。"""
        day_only = QuotaRules(day=LimitRule(window_seconds=86400, max_count=1))

        async def _stub(t, a, b):
            return day_only, "test"

        monkeypatch.setattr(repo_mod, "load_rules", _stub)
        routes_mod.repository.load_rules = _stub

        await client.post(
            "/v1/quota/check-strict",
            json={"tenant_id": "t1", "app_id": "app_x", "api_id": "api_y"},
        )
        r = await client.post(
            "/v1/quota/check-strict",
            json={"tenant_id": "t1", "app_id": "app_x", "api_id": "api_y"},
        )
        assert r.status_code == 429
        assert r.json()["code"] == 20004  # TENANT_QUOTA_EXCEEDED


# ========== /v1/quota/refund ==========


class TestRefund:
    async def test_refund_decrements(self, client, fake_redis, stub_rules, authed):
        """扣 1 → 退 1 → usage 显示 0。

        用 usage 端点直接验证（不绕弯子靠 blocked 状态推断）。
        """
        # 调一次（扣 1）
        await client.post(
            "/v1/quota/check",
            json={"tenant_id": "t1", "app_id": "app_x", "api_id": "api_u"},
        )

        # 退 1
        refund_resp = await client.post(
            "/v1/quota/refund",
            json={"tenant_id": "t1", "app_id": "app_x", "api_id": "api_u"},
        )
        assert refund_resp.status_code == 200
        assert refund_resp.json()["refunded"] is True

        # 查 usage 应该是 0
        usage = await client.get(
            "/v1/quota/usage",
            params={"tenant_id": "t1", "app_id": "app_x", "api_id": "api_u"},
            headers={"X-API-Key": "ak_test"},
        )
        assert usage.json()["second"]["used"] == 0

    async def test_refund_unblocks_after_failure(self, client, fake_redis, stub_rules, monkeypatch):
        """业务场景：调用失败 → refund 后下次能 retry。

        关键：被挡的那次 INCR 已生效（设计如此，参见 limiter.py 注释），
        所以 refund 必须 >= cost 才能解锁。
        """
        # 调用 1：成功（扣 1，used=1）
        await client.post(
            "/v1/quota/check",
            json={"tenant_id": "t1", "app_id": "app_x", "api_id": "api_u"},
        )
        # 调用 2：成功（扣 1，used=2，max=2）
        await client.post(
            "/v1/quota/check",
            json={"tenant_id": "t1", "app_id": "app_x", "api_id": "api_u"},
        )
        # 调用 3：失败（INCR→3 但 blocked，业务后端报错）
        await client.post(
            "/v1/quota/check",
            json={"tenant_id": "t1", "app_id": "app_x", "api_id": "api_u"},
        )

        # 业务失败 → 退回 cost=1
        await client.post(
            "/v1/quota/refund",
            json={"tenant_id": "t1", "app_id": "app_x", "api_id": "api_u"},
        )
        # 现在 used=2，下一次调用 INCR→3 仍超。需要再退一次才能解锁。
        # 实际生产用 cost=2 退（含被挡那次）：模拟这种情况。
        await client.post(
            "/v1/quota/refund",
            json={"tenant_id": "t1", "app_id": "app_x", "api_id": "api_u"},
        )

        # 现在 used=1，下次调用 INCR→2 通过
        r = await client.post(
            "/v1/quota/check",
            json={"tenant_id": "t1", "app_id": "app_x", "api_id": "api_u"},
        )
        assert r.json()["allowed"] is True


# ========== /v1/quota/usage ==========


class TestUsage:
    async def test_requires_auth(self, client, fake_redis, stub_rules):
        """usage 需要鉴权。"""
        r = await client.get(
            "/v1/quota/usage",
            params={"tenant_id": "t1", "app_id": "app_x", "api_id": "api_u"},
        )
        assert r.status_code == 401

    async def test_returns_usage(self, client, fake_redis, stub_rules, authed):
        r = await client.get(
            "/v1/quota/usage",
            params={"tenant_id": "t1", "app_id": "app_x", "api_id": "api_u"},
            headers={"X-API-Key": "ak_test"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["tenant_id"] == "t1"
        assert "second" in body
        assert "minute" in body
        assert "day" in body
        assert body["second"]["used"] == 0

    async def test_cannot_view_other_tenant(self, client, fake_redis, stub_rules, monkeypatch):
        """非超管不能查别的租户。"""

        async def _auth_other(request, settings, api_key, required_scopes=None):
            ctx = TenantContext(
                tenant_id="t2",
                tenant_type="internal",
                app_id="app_y",
            )
            set_tenant_context(ctx)
            return ctx

        monkeypatch.setattr(core_auth, "authenticate_request", _auth_other)

        r = await client.get(
            "/v1/quota/usage",
            params={"tenant_id": "t1", "app_id": "app_x", "api_id": "api_u"},
            headers={"X-API-Key": "ak_test"},
        )
        assert r.status_code == 403


# ========== /v1/quota/billing ==========


class TestBilling:
    async def test_billing_returns_placeholder(self, client, fake_redis, authed):
        from apihub_core import db

        if getattr(db, "_pool", None) is None:
            pytest.skip("billing needs PG (subscription query); pool not initialized")
        r = await client.get(
            "/v1/quota/billing",
            params={"tenant_id": "t1", "month": "2026-07"},
            headers={"X-API-Key": "ak_test"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "not_implemented"
        assert body["month"] == "2026-07"


# ========== /v1/quota/health ==========


class TestHealth:
    async def test_health_returns_ok(self, client):
        r = await client.get("/v1/quota/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok", "service": "quota"}
