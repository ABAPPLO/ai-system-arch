"""backoff 算法测试。"""

from retry_svc.backoff import compute_delay_ms, next_attempt_delay_ms
from retry_svc.models import BackoffPolicy


class TestComputeDelay:
    def test_exponential_doubles(self):
        """attempt_no=1 → base, attempt_no=2 → ~2x, attempt_no=3 → ~4x。"""
        d1 = compute_delay_ms(1, policy=BackoffPolicy.EXPONENTIAL, base_ms=1000)
        d2 = compute_delay_ms(2, policy=BackoffPolicy.EXPONENTIAL, base_ms=1000)
        d3 = compute_delay_ms(3, policy=BackoffPolicy.EXPONENTIAL, base_ms=1000)

        # jitter ±25%：d1 ∈ [750, 1250], d2 ∈ [1500, 2500], d3 ∈ [3000, 5000]
        assert 750 <= d1 <= 1250
        assert 1500 <= d2 <= 2500
        assert 3000 <= d3 <= 5000

    def test_fixed_constant(self):
        d1 = compute_delay_ms(1, policy=BackoffPolicy.FIXED, base_ms=2000)
        d2 = compute_delay_ms(5, policy=BackoffPolicy.FIXED, base_ms=2000)
        assert d1 == 2000
        assert d2 == 2000

    def test_linear_grows(self):
        d1 = compute_delay_ms(1, policy=BackoffPolicy.LINEAR, base_ms=1000)
        d2 = compute_delay_ms(2, policy=BackoffPolicy.LINEAR, base_ms=1000)
        d3 = compute_delay_ms(3, policy=BackoffPolicy.LINEAR, base_ms=1000)
        assert d1 == 1000
        assert d2 == 2000
        assert d3 == 3000

    def test_cap(self):
        """超过 cap_ms 限制。"""
        d = compute_delay_ms(20, policy=BackoffPolicy.EXPONENTIAL, base_ms=1000, cap_ms=60_000)
        assert d == 60_000

    def test_invalid_attempt_no_clamped(self):
        """attempt_no=0 / -1 不应炸（clamped to 1）。"""
        d = compute_delay_ms(0, policy=BackoffPolicy.FIXED, base_ms=500)
        assert d == 500

    def test_str_policy_accepted(self):
        """str 形式的 policy 也接受（DB 取出来是 str）。"""
        d = compute_delay_ms(1, policy="fixed", base_ms=500)
        assert d == 500


class TestNextAttemptDelay:
    def test_retry_count_zero(self):
        """0 次失败 → 第一次 attempt 延迟 = base。"""
        d = next_attempt_delay_ms(0, policy=BackoffPolicy.FIXED, base_ms=1000)
        assert d == 1000

    def test_retry_count_increments(self):
        """2 次失败 → 第 3 次 attempt = 4x base（exponential + jitter）。"""
        d = next_attempt_delay_ms(2, policy=BackoffPolicy.EXPONENTIAL, base_ms=1000)
        # 3rd attempt = 1000 * 2^2 = 4000 ± 25%
        assert 3000 <= d <= 5000
