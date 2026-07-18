package limiter

import (
	"context"
	"fmt"
	"math"
	"os"
	"strconv"
	"time"

	"github.com/redis/go-redis/v9"

	"github.com/abapplo/apihub/services/go/quota/internal/models"
)

// Lua scripts — ported VERBATIM from Python
// services/services/quota/src/quota/lua_scripts.py. Redis executes Lua
// atomically (single-threaded), so multi-tier check + INCR + EXPIRE runs in a
// single RTT with no cross-tier race window. Same KEYS / ARGV layout, same
// return shape — see also docs/06-high-concurrency.md §7.3.
//
// CHECK_AND_INCR contract (Python lua_scripts.py:26-47):
//
//	KEYS[1..n]      N tier keys (order: second, minute, day)
//	ARGV[1..n]      each tier's max_count (0 = skip check, still INCR for usage)
//	ARGV[n+1..2n]   each tier's TTL (seconds)
//	ARGV[2n+1]      cost (INCRBY amount)
//
// Returns:
//
//	{0, 0, 0}                 all tiers passed
//	{tier_idx, ttl, current}  tier_idx (1-based) exceeded; current = post-INCR count
const checkAndIncrScript = `
local n = #KEYS
local cost = tonumber(ARGV[2 * n + 1]) or 1

for i = 1, n do
    local max_count = tonumber(ARGV[i])
    local ttl = tonumber(ARGV[n + i])

    local current = redis.call('INCRBY', KEYS[i], cost)
    if current == cost then
        redis.call('EXPIRE', KEYS[i], ttl)
    end

    if max_count > 0 and current > max_count then
        local remaining_ttl = redis.call('TTL', KEYS[i])
        if remaining_ttl < 0 then remaining_ttl = ttl end
        return {i, remaining_ttl, current}
    end
end

return {0, 0, 0}
`

// REFUND contract (Python lua_scripts.py:52-64): atomic DecrBy clamped at 0
// across all tier keys. ARGV[1] = cost. Returns 1. The key is NOT deleted
// (window is preserved); only the counter rewinds.
const refundScript = `
local n = #KEYS
local cost = tonumber(ARGV[1])

for i = 1, n do
    local current = tonumber(redis.call('GET', KEYS[i]) or '0')
    local new_val = current - cost
    if new_val < 0 then new_val = 0 end
    redis.call('SET', KEYS[i], new_val)
end

return 1
`

// Compiled scripts — Script caches the SHA-1 digest and prefers EVALSHA,
// transparently falling back to EVAL on NOSCRIPT (see go-redis script.go:193).
var (
	checkAndIncr = redis.NewScript(checkAndIncrScript)
	refund       = redis.NewScript(refundScript)
)

// Canonical tier windows in SECONDS — matches Python _TIER_DEFS
// (TIER_SECOND=1, TIER_MINUTE=60, TIER_DAY=86400). Used for both slot
// computation and Lua TTL args. Rule rows carry per-tier windows too but the
// Python contract compiles only the canonical windows into Lua args
// (limiter.py:_compile_rules — `ttls.append(window)` where window comes from
// _TIER_DEFS, NOT from the rule row).
const (
	tierSecondSecs int64 = 1
	tierMinuteSecs int64 = 60
	tierDaySecs    int64 = 86400
)

// Limiter implements rate limiting with Redis fixed-window counters.
//
// Redis key layout (aligned with the Python contract,
// services/services/quota/src/quota/limiter.py::_rate_keys):
//
//	t:{tenant_id}:rate:{api_id}:{app_id}:{tier}:{slot}
//
// where tier ∈ {"s","m","d"} and slot = now_unix / window_seconds.
//
// The region / splitRatio fields are kept as R3b prep (multi-region quota
// sharing) but are NOT used in the key in R3a — splitRatio defaults to 1.0
// and the key omits the region segment so Go and Python share the same
// counters.
type Limiter struct {
	redis      *redis.Client
	region     string
	splitRatio float64
}

// New creates a Limiter backed by the provided Redis client.
// An empty region falls back to the HOME_REGION env var.
// A zero or negative splitRatio falls back to QUOTA_REGION_SPLIT_RATIO
// (default 1.0 in R3a — no split).
func New(rdb *redis.Client, region string, splitRatio float64) *Limiter {
	if region == "" {
		region = os.Getenv("HOME_REGION")
	}
	if splitRatio <= 0 {
		splitRatio = parseFloat64(os.Getenv("QUOTA_REGION_SPLIT_RATIO"), 1.0)
	}
	return &Limiter{
		redis:      rdb,
		region:     region,
		splitRatio: splitRatio,
	}
}

// HealthCheck pings the underlying Redis client. Used by the
// /health/ready handler (alongside repository.PGRepository.HealthCheck) so
// k8s readiness gates on both stores being reachable.
func (l *Limiter) HealthCheck(ctx context.Context) error {
	if l == nil || l.redis == nil {
		return fmt.Errorf("redis client not initialized")
	}
	return l.redis.Ping(ctx).Err()
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

func parseFloat64(s string, def float64) float64 {
	if s == "" {
		return def
	}
	v, err := strconv.ParseFloat(s, 64)
	if err != nil {
		return def
	}
	return v
}

// rateKey returns the Redis key for a rate-counter slot, matching the Python
// contract: t:{tenant}:rate:{api}:{app}:{tier}:{slot}. tier is the single-char
// tier tag ("s" / "m" / "d"). The receiver's region field is intentionally
// unused (R3a: splitRatio=1, no region prefix; R3b will revisit).
func (l *Limiter) rateKey(tenantID, apiID, appID, tier, slot string) string {
	return fmt.Sprintf("t:%s:rate:%s:%s:%s:%s", tenantID, apiID, appID, tier, slot)
}

// tierChar maps a tier name ("second"/"minute"/"day") to its single-char
// Redis key segment ("s"/"m"/"d").
func tierChar(name string) string {
	switch name {
	case "second":
		return "s"
	case "minute":
		return "m"
	case "day":
		return "d"
	}
	return "s"
}

// effectiveQuota applies the region split ratio to the rule's MaxCount.
// In R3a splitRatio=1.0 so this is a no-op; the field is reserved for R3b.
func (l *Limiter) effectiveQuota(rule models.LimitRule) int64 {
	return int64(math.Ceil(float64(rule.MaxCount) * l.splitRatio))
}

// slotIndex returns the sliding-window slot string for now and windowMs.
func slotIndex(now time.Time, windowMs int64) string {
	if windowMs <= 0 {
		return "0"
	}
	return strconv.FormatInt(now.UnixMilli()/windowMs, 10)
}

// rateKeysForTiers returns the three canonical tier Redis keys for the given
// instant, matching Python _rate_keys verbatim (canonical windows, slot =
// ts//window_seconds). Order MUST be [second, minute, day] — that is the KEYS
// order the CHECK_AND_INCR script expects (Lua indexes KEYS[1..n] by tier).
func (l *Limiter) rateKeysForTiers(tenantID, apiID, appID string, now time.Time) []string {
	ts := now.Unix()
	return []string{
		l.rateKey(tenantID, apiID, appID, "s", strconv.FormatInt(ts/tierSecondSecs, 10)),
		l.rateKey(tenantID, apiID, appID, "m", strconv.FormatInt(ts/tierMinuteSecs, 10)),
		l.rateKey(tenantID, apiID, appID, "d", strconv.FormatInt(ts/tierDaySecs, 10)),
	}
}

// toInt64 coerces a Lua return-shelf element to int64. go-redis serves Lua
// integers as int64 in production; miniredis occasionally surfaces them as a
// different numeric go-type, so we defensively accept the union.
func toInt64(v interface{}) (int64, bool) {
	switch x := v.(type) {
	case int64:
		return x, true
	case int:
		return int64(x), true
	case int32:
		return int64(x), true
	case float64:
		return int64(x), true
	case string:
		n, err := strconv.ParseInt(x, 10, 64)
		return n, err == nil
	}
	return 0, false
}

// ---------------------------------------------------------------------------
// Public API — used by handler.QuotaHandler
// ---------------------------------------------------------------------------

// CheckAndConsume performs an atomic Lua check+incr across all three tiers
// (one Eval, one RTT) and returns the merged response. Field semantics mirror
// the Python contract (services/services/quota/src/quota/limiter.py::
// check_and_consume):
//
//   - rule_source="unlimited": no tier has a positive MaxCount → return
//     immediately without touching Redis (Python limiter.py:79-83).
//   - rule_source="rules", Allowed=true: all tiers passed; Remaining is
//     min(active max) - cost per Python _remaining_for_first_active
//     (limiter.py:230-239), or nil when there are no active tiers (this
//     branch is unreachable here because the unlimited short-circuit fires
//     first, but the guard keeps the function total).
//   - rule_source="rules", Allowed=false: TierBlocked + Limit + RetryAfterSeconds
//     set from the Lua return tuple {tier_idx, ttl, current}; Remaining nil
//     (Python limiter.py:135-141).
//   - rule_source="fallback": Redis Eval failed → conservative allow with no
//     counters (Python limiter.py:97-108).
//
// TierBlocked / Limit / Remaining are *int / *string so they marshal as JSON
// null when unset, matching Python's pydantic Optional semantics — see
// models.QuotaCheckResponse.
func (l *Limiter) CheckAndConsume(ctx context.Context, tenantID, appID, apiID string, rules *models.QuotaRules, cost int) *models.QuotaCheckResponse {
	tiers := [3]struct {
		name string
		rule models.LimitRule
	}{
		{"second", rules.Second},
		{"minute", rules.Minute},
		{"day", rules.Day},
	}

	// Unlimited short-circuit: no active tier → don't touch Redis.
	hasActive := false
	for _, t := range tiers {
		if t.rule.MaxCount > 0 {
			hasActive = true
			break
		}
	}
	if !hasActive {
		return &models.QuotaCheckResponse{
			Allowed:    true,
			RuleSource: "unlimited",
		}
	}

	// Args layout per Python _compile_rules: [max_s, max_m, max_d, ttl_s,
	// ttl_m, ttl_d, cost]. max=0 for inactive tiers — Lua skips the check
	// but still INCRs so GET /usage reflects the call.
	keys := l.rateKeysForTiers(tenantID, apiID, appID, time.Now())
	args := []interface{}{
		l.effectiveQuota(rules.Second), l.effectiveQuota(rules.Minute), l.effectiveQuota(rules.Day),
		tierSecondSecs, tierMinuteSecs, tierDaySecs,
		cost,
	}

	res, err := checkAndIncr.Run(ctx, l.redis, keys, args...).Result()
	if err != nil {
		// Redis error → conservative allow (matches Python limiter.py:97-108).
		return &models.QuotaCheckResponse{
			Allowed:           true,
			RuleSource:        "fallback",
			RetryAfterSeconds: 0,
		}
	}

	arr, ok := res.([]interface{})
	if !ok || len(arr) < 3 {
		// Unexpected return shape — treat as fallback rather than crashing
		// the request path. Logs at the handler layer; here we degrade safe.
		return &models.QuotaCheckResponse{Allowed: true, RuleSource: "fallback"}
	}

	tierIdx, _ := toInt64(arr[0])
	ttlSec, _ := toInt64(arr[1])

	if tierIdx == 0 {
		// All passed — compute Remaining for the strictest active tier.
		rem := l.minActiveRemaining(rules, cost)
		return &models.QuotaCheckResponse{
			Allowed:    true,
			Remaining:  rem,
			RuleSource: "rules",
		}
	}

	// Blocked at tier_idx (1-based). T1's pointer types: TierBlocked + Limit
	// set, Remaining nil, RetryAfterSeconds from Lua ttl (Python L135-141).
	idx := int(tierIdx) - 1
	if idx < 0 || idx >= len(tiers) {
		return &models.QuotaCheckResponse{Allowed: true, RuleSource: "fallback"}
	}
	blocked := tiers[idx]
	tb := blocked.name
	limit := int(l.effectiveQuota(blocked.rule))
	retryAfter := int(ttlSec)
	if retryAfter < 0 {
		retryAfter = 0
	}
	return &models.QuotaCheckResponse{
		Allowed:           false,
		TierBlocked:       &tb,
		Limit:             &limit,
		Remaining:         nil,
		RetryAfterSeconds: retryAfter,
		RuleSource:        "rules",
	}
}

// minActiveRemaining returns min(active tier max) - cost, or nil if no tier is
// active. Mirrors Python _remaining_for_first_active (limiter.py:230-239).
// On the allowed path cost <= every active max (otherwise Lua would have
// blocked on the first INCRBY), so the clamp at 0 is defensive only.
func (l *Limiter) minActiveRemaining(rules *models.QuotaRules, cost int) *int {
	var minMax int64 = -1
	for _, r := range [3]models.LimitRule{rules.Second, rules.Minute, rules.Day} {
		if r.MaxCount <= 0 {
			continue
		}
		q := l.effectiveQuota(r)
		if minMax < 0 || q < minMax {
			minMax = q
		}
	}
	if minMax < 0 {
		return nil
	}
	rem := int(minMax) - cost
	if rem < 0 {
		rem = 0
	}
	return &rem
}

// Refund atomically rewinds cost across all three tier counters via the Lua
// REFUND script (one Eval). Mirrors Python limiter.refund (limiter.py:144-166):
// failure is swallowed — refund is best-effort and never blocks business flow.
// Counters are clamped at 0 by the script; the key is preserved so the window
// keeps its TTL.
func (l *Limiter) Refund(ctx context.Context, tenantID, appID, apiID string, cost int) bool {
	keys := l.rateKeysForTiers(tenantID, apiID, appID, time.Now())
	_, err := refund.Run(ctx, l.redis, keys, cost).Result()
	return err == nil
}

// GetUsage returns the current flat usage for the three canonical tiers,
// matching Python's UsageResponse shape: {tenant_id, app_id, api_id, second,
// minute, day}. Each UsagePoint always reports window_seconds + used; limit
// is non-nil only when the tier is configured (mirrors Python's None default).
//
// WindowSeconds is ALWAYS the canonical _TIER_DEFS value (1 / 60 / 86400),
// regardless of what WindowSec the rule row carries — see Python
// limiter.py:190-196 (`window_seconds=window` where `window` comes from
// `_TIER_DEFS`, not from the rule).
func (l *Limiter) GetUsage(ctx context.Context, tenantID, appID, apiID string, rules *models.QuotaRules) *models.UsageResponse {
	now := time.Now()
	resp := &models.UsageResponse{
		TenantID: tenantID,
		AppID:    appID,
		APIID:    apiID,
	}

	tiers := []struct {
		name         string
		rule         models.LimitRule
		canonicalSec int64
		canonicalMs  int64
	}{
		{"second", rules.Second, 1, 1000},
		{"minute", rules.Minute, 60, 60000},
		{"day", rules.Day, 86400, 86400000},
	}

	for _, t := range tiers {
		point := models.UsagePoint{
			WindowSeconds: t.canonicalSec,
			Used:          0,
		}

		if t.rule.MaxCount > 0 {
			limit := int(l.effectiveQuota(t.rule))
			point.Limit = &limit
		}

		// Read from the canonical-window slot (matches Python's _rate_keys,
		// which always uses TIER_SECOND/MINUTE/DAY for slot computation).
		key := l.rateKey(tenantID, apiID, appID, tierChar(t.name), slotIndex(now, t.canonicalMs))
		if count, err := l.redis.Get(ctx, key).Int(); err == nil {
			point.Used = count
		}

		switch t.name {
		case "second":
			resp.Second = point
		case "minute":
			resp.Minute = point
		case "day":
			resp.Day = point
		}
	}

	return resp
}
