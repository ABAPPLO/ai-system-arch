import pytest
import billing.billing_job as billing_job
from billing.billing_job import _parse_period, _calc_overage

class TestBillingCore:
    def test_parse_period(self):
        s, e = _parse_period("2026-07")
        assert s.year == 2026 and s.month == 7 and s.day == 1
        assert e.month == 8

    def test_no_overage(self):
        assert _calc_overage(100, 200, 5, 1000) == 0

    def test_with_overage(self):
        assert _calc_overage(1500, 1000, 5, 1000) == 5

    def test_multi_unit(self):
        assert _calc_overage(3500, 1000, 5, 1000) == 15

    @pytest.mark.asyncio
    async def test_dry_run_no_subs(self, monkeypatch):
        async def mock_list(): return []
        monkeypatch.setattr("billing.billing_job.list_active_subscriptions", mock_list)
        result = await billing_job.run_billing(period="2026-07", dry_run=True)
        assert result.total_tenants == 0
