import pytest
from apihub_core.tenant import TenantContext, set_tenant_context


@pytest.mark.asyncio
async def test_health(client):
    resp = await client.get("/health/live")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_periodic_dry_run(client, monkeypatch):
    from apihub_core import auth as auth_mod

    ctx = TenantContext(tenant_id="42", tenant_type="internal", user_id="u_test")

    async def mock_auth(request, settings, api_key, **kw):
        set_tenant_context(ctx)
        return ctx

    monkeypatch.setattr(auth_mod, "authenticate_request", mock_auth)

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
