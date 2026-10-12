import billing.billing_job as billing_job
import pytest
from billing.billing_job import _calc_overage, _parse_period


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
        async def mock_list():
            return []

        monkeypatch.setattr("billing.billing_job.list_active_subscriptions", mock_list)
        result = await billing_job.run_billing(period="2026-07", dry_run=True)
        assert result.total_tenants == 0


def _sub(tenant_id="t1", calls_per_day=100, price=99900, overage_prices=None):
    """构造一个 active 订阅（对齐 SubscriptionInfo 真实字段）。"""
    from billing.models import SubscriptionInfo

    return SubscriptionInfo(
        tenant_id=tenant_id,
        subscription_id=7,
        plan_code="starter",
        plan_name="Starter",
        overage_unit_price=overage_prices,
        quota_included={"calls_per_day": calls_per_day, "tokens_per_month": 1000},
        price_cents=price,
    )


class TestRunBillingRealPath:
    """真实路径回归：此前 CH 查询必被租户护栏 ValueError、INSERT 列名不存在。"""

    @pytest.mark.asyncio
    async def test_ch_query_admin_scope_and_params(self, monkeypatch):
        """CH 查询必须显式 force_tenant_id=None + %(tenant_id)s 参数名。"""
        captured = {}

        def mock_query_all(sql, params=None, *, force_tenant_id="sentinel"):
            captured["sql"] = sql
            captured["params"] = params
            captured["force"] = force_tenant_id
            return [{"calls": 10, "tokens": 20}]

        async def mock_list():
            return [_sub()]

        monkeypatch.setattr("billing.billing_job.query_all", mock_query_all)
        monkeypatch.setattr("billing.billing_job.list_active_subscriptions", mock_list)
        result = await billing_job.run_billing(period="2026-07", dry_run=True)

        assert captured["force"] is None  # admin 域（sentinel 会被护栏拒）
        assert "tenant_id" in captured["params"]  # %(tenant_id)s 绑定
        assert result.total_tenants == 1
        assert result.records[0].total_calls == 10

    @pytest.mark.asyncio
    async def test_insert_uses_real_schema_and_subscription(self, monkeypatch):
        """INSERT 须带 subscription_id / period_start / period_end（表 NOT NULL）。"""
        inserted = {}

        async def mock_insert(**kw):
            inserted.update(kw)
            return "1"

        def mock_query_all(sql, params=None, *, force_tenant_id=None):
            return [{"calls": 5, "tokens": 5}]

        async def mock_update_sub(tid, s, e):
            inserted["sub_new"] = (s, e)

        async def mock_list():
            return [_sub()]

        async def mock_insert_job(period, status="running"):
            return "job-1"

        async def mock_update_job(job_id, **kw):
            inserted["job"] = kw

        monkeypatch.setattr("billing.billing_job.query_all", mock_query_all)
        monkeypatch.setattr("billing.billing_job.list_active_subscriptions", mock_list)
        monkeypatch.setattr("billing.billing_job.insert_billing_record", mock_insert)
        monkeypatch.setattr("billing.billing_job.update_subscription_period", mock_update_sub)
        monkeypatch.setattr("billing.billing_job.insert_job_log", mock_insert_job)
        monkeypatch.setattr("billing.billing_job.update_job_log", mock_update_job)

        async def _no_job(period):
            return False

        monkeypatch.setattr("billing.billing_job.check_job_exists", _no_job)

        await billing_job.run_billing(period="2026-07", dry_run=False)

        assert inserted["subscription_id"] == 7
        assert inserted["period_start"].month == 7 and inserted["period_start"].day == 1
        assert inserted["period_end"].month == 8 and inserted["period_end"].day == 1
        # 账期推进到自然月边界（8/1 → 9/1），不再是 +30 天漂移
        assert inserted["sub_new"][0].month == 8 and inserted["sub_new"][1].month == 9
        assert inserted["job"]["status"] == "done"

    @pytest.mark.asyncio
    async def test_tenant_failure_continues_and_marks_failed(self, monkeypatch):
        """单租户失败：跳过继续，job 标 failed（不再 raise 炸整批）。"""
        calls = []

        def mock_query_all(sql, params=None, *, force_tenant_id=None):
            tenant = params["tenant_id"]
            calls.append(tenant)
            if tenant == "bad":
                raise RuntimeError("ch down")
            return [{"calls": 1, "tokens": 1}]

        async def mock_list():
            return [_sub("good"), _sub("bad")]

        job_log = {}

        async def mock_insert_job(period, status="running"):
            return "job-1"

        async def mock_update_job(job_id, **kw):
            job_log.update(kw)

        monkeypatch.setattr("billing.billing_job.query_all", mock_query_all)
        monkeypatch.setattr("billing.billing_job.list_active_subscriptions", mock_list)
        monkeypatch.setattr("billing.billing_job.insert_job_log", mock_insert_job)
        monkeypatch.setattr("billing.billing_job.update_job_log", mock_update_job)

        async def _no_job(period):
            return False

        async def _mock_insert_record(**kw):
            return "1"

        async def _mock_update_sub(*a):
            return None

        monkeypatch.setattr("billing.billing_job.check_job_exists", _no_job)
        monkeypatch.setattr("billing.billing_job.insert_billing_record", _mock_insert_record)
        monkeypatch.setattr("billing.billing_job.update_subscription_period", _mock_update_sub)

        result = await billing_job.run_billing(period="2026-07", dry_run=False)
        assert calls == ["good", "bad"]  # bad 失败后没有中断（循环继续过了）
        assert result.total_tenants == 1  # good 出账成功
        assert job_log["status"] == "failed"
        assert "bad" in job_log["error_msg"]

    @pytest.mark.asyncio
    async def test_quota_by_actual_days(self, monkeypatch):
        """配额 = calls_per_day × 账期实际天数（2026-07 有 31 天，不是 30）。"""

        def mock_query_all(sql, params=None, *, force_tenant_id=None):
            return [{"calls": 100 * 31, "tokens": 0}]  # 恰好打满

        async def mock_list():
            return [_sub(calls_per_day=100)]

        monkeypatch.setattr("billing.billing_job.query_all", mock_query_all)
        monkeypatch.setattr("billing.billing_job.list_active_subscriptions", mock_list)
        result = await billing_job.run_billing(period="2026-07", dry_run=True)
        assert result.records[0].quota_calls == 3100
        assert result.records[0].overage_calls == 0  # 打满不超额

    @pytest.mark.asyncio
    async def test_overage_price_from_plan(self, monkeypatch):
        """超额单价优先读 plan.overage_unit_price（不再硬编码 5/10）。"""

        def mock_query_all(sql, params=None, *, force_tenant_id=None):
            # 31 天 × 30/天 = 930 配额，实耗 1930 → 超 1000（恰 1 单位）
            return [{"calls": 1930, "tokens": 0}]

        async def mock_list():
            return [_sub(calls_per_day=30, overage_prices={"calls_per_1000": 20})]

        monkeypatch.setattr("billing.billing_job.query_all", mock_query_all)
        monkeypatch.setattr("billing.billing_job.list_active_subscriptions", mock_list)
        result = await billing_job.run_billing(period="2026-07", dry_run=True)
        assert result.records[0].overage_cents == 20  # 1 千次 × 20 cents

    def test_next_month_start(self):
        from datetime import UTC, datetime

        f = billing_job._next_month_start
        assert f(datetime(2026, 7, 1, tzinfo=UTC)).month == 8
        assert f(datetime(2026, 12, 1, tzinfo=UTC)).year == 2027
