"""rules 合并测试 —— app > tenant > api_version > default。"""


from quota.models import QuotaRules
from quota.repository import _merge, _parse_rules_blob, _parse_tier


class TestParseTier:
    def test_none_returns_none(self):
        assert _parse_tier(None, 60) is None

    def test_int_shorthand(self):
        """100 → max=100, window=default。"""
        rule = _parse_tier(100, 60)
        assert rule.max_count == 100
        assert rule.window_seconds == 60

    def test_full_dict(self):
        rule = _parse_tier(
            {"max_count": 1000, "window_seconds": 86400, "enabled": True},
            86400,
        )
        assert rule.max_count == 1000
        assert rule.window_seconds == 86400
        assert rule.enabled is True

    def test_shorthand_keys(self):
        """max / count 别名都能识别。"""
        for key in ("max", "count", "max_count"):
            r = _parse_tier({key: 50}, 60)
            assert r.max_count == 50

    def test_invalid_returns_none(self):
        assert _parse_tier("garbage", 60) is None
        assert _parse_tier({"foo": "bar"}, 60) is None
        assert _parse_tier([], 60) is None


class TestParseRulesBlob:
    def test_empty_dict(self):
        rules = _parse_rules_blob({})
        assert rules.second is None
        assert rules.minute is None
        assert rules.day is None

    def test_json_string(self):
        rules = _parse_rules_blob(
            '{"second": {"max_count": 10}, "minute": 100, "day": 10000}'
        )
        assert rules.second.max_count == 10
        assert rules.minute.max_count == 100
        assert rules.day.max_count == 10000

    def test_garbage_json(self):
        """非 JSON 字符串不应崩。"""
        rules = _parse_rules_blob("not json")
        assert rules == QuotaRules()

    def test_partial(self):
        """只配 minute 也 OK。"""
        rules = _parse_rules_blob({"minute": {"max_count": 100}})
        assert rules.second is None
        assert rules.minute.max_count == 100
        assert rules.day is None


class TestMerge:
    def test_override_wins(self):
        base = QuotaRules(second=None, minute=None, day=None)
        override = QuotaRules(
            second=_parse_tier(10, 1),
            minute=None,
            day=None,
        )
        merged = _merge(base, override)
        assert merged.second.max_count == 10

    def test_per_tier_independent(self):
        """每 tier 独立合并，互不影响。"""
        base = QuotaRules(
            second=_parse_tier(10, 1),
            minute=_parse_tier(100, 60),
            day=_parse_tier(10000, 86400),
        )
        override = QuotaRules(
            second=_parse_tier(20, 1),   # 只 override second
            minute=None,
            day=None,
        )
        merged = _merge(base, override)
        assert merged.second.max_count == 20    # override
        assert merged.minute.max_count == 100   # base
        assert merged.day.max_count == 10000    # base

    def test_chain_three_layers(self):
        """app > tenant > api_version 链式合并。"""
        api_rules = _parse_rules_blob({"second": 5, "minute": 50, "day": 5000})
        tenant_rules = _parse_rules_blob({"minute": 100})      # 提升 minute
        app_rules = _parse_rules_blob({"day": 50000})          # 提升 day

        merged = _merge(_merge(api_rules, tenant_rules), app_rules)
        # app 提了 day
        assert merged.day.max_count == 50000
        # tenant 提了 minute
        assert merged.minute.max_count == 100
        # 没人提 second，保留 api 的
        assert merged.second.max_count == 5
