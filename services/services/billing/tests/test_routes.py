import pytest
from apihub_core.tenant import TenantContext, set_tenant_context


@pytest.mark.asyncio
async def test_health(client):
    resp = await client.get("/health/live")
    assert resp.status_code == 200


def _auth_as(monkeypatch, *, is_admin: bool):
    """把鉴权 stub 成（非）超管租户上下文。"""
    from apihub_core import auth as auth_mod

    ctx = TenantContext(
        tenant_id="42",
        tenant_type="internal",
        user_id="u_test",
        is_platform_admin=is_admin,
    )

    async def mock_auth(request, settings, api_key, **kw):
        set_tenant_context(ctx)
        return ctx

    monkeypatch.setattr(auth_mod, "authenticate_request", mock_auth)


@pytest.mark.asyncio
async def test_periodic_dry_run(client, monkeypatch):
    _auth_as(monkeypatch, is_admin=True)

    async def mock_run(**kw):
        from billing.models import BillingJobResult

        return BillingJobResult(period="2026-07", total_tenants=0)

    monkeypatch.setattr("billing.routes.billing_job.run_billing", mock_run)
    resp = await client.post(
        "/v1/billing/periodic?dry_run=true",
        headers={"X-API-Key": "ak_test"},
    )
    assert resp.status_code == 200
    assert resp.json()["period"] == "2026-07"


@pytest.mark.asyncio
async def test_periodic_forbidden_for_normal_tenant(client, monkeypatch):
    """普通租户 Key 触发全局出账 → 403（业务函数绝不能被调到）。"""
    _auth_as(monkeypatch, is_admin=False)

    called = []

    async def mock_run(**kw):
        called.append(1)
        raise AssertionError("must not be called")

    monkeypatch.setattr("billing.routes.billing_job.run_billing", mock_run)
    resp = await client.post(
        "/v1/billing/periodic?dry_run=true",
        headers={"X-API-Key": "ak_test"},
    )
    assert resp.status_code == 403
    assert called == []


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/v1/admin/billing/summary"),
        ("POST", "/v1/admin/billing/adjust"),
        ("POST", "/v1/admin/subscription/override"),
    ],
)
@pytest.mark.asyncio
async def test_admin_endpoints_forbidden_for_normal_tenant(client, monkeypatch, method, path):
    """billing 的 3 个 /admin 端点对普通租户 Key 一律 403。"""
    _auth_as(monkeypatch, is_admin=False)

    body = {
        "POST /v1/admin/billing/adjust": {"record_id": "r1", "delta_cents": -100, "reason": "x"},
        "POST /v1/admin/subscription/override": {
            "tenant_id": "42",
            "plan_code": "pro",
            "reason": "test",
        },
    }
    resp = await client.request(
        method, path, json=body.get(f"{method} {path}"), headers={"X-API-Key": "ak_test"}
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_billing_records_reaches_repository(client, monkeypatch):
    """/v1/billing/records 修复 ImportError 后应正常走到 repository（stub 掉 DB 层）。"""
    _auth_as(monkeypatch, is_admin=False)

    async def mock_records(tenant_id, limit, offset):
        assert tenant_id == "42"
        return ([], 0)

    monkeypatch.setattr("billing.routes.get_billing_records", mock_records)
    resp = await client.get("/v1/billing/records", headers={"X-API-Key": "ak_test"})
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "total": 0, "limit": 12, "offset": 0}
