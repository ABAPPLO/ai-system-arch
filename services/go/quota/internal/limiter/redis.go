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

// Limiter implements rate limiting with Redis fixed-window counters.
//
// Redis key layout (aligned with the Python contract,
// services/services/quota/src/quota/limiter.py::_rate_keys):
//
//	t:{tenant_id}:rate:{api_id}:{app_id}:{tier}:{slot}
//
// where tier ∈ {"s","m","d"} and slot = now_ms / window_ms.
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

// ---------------------------------------------------------------------------
// Public API — used by handler.QuotaHandler
// ---------------------------------------------------------------------------

// CheckAndConsume increments counters for every configured tier and returns
// the merged response. Field semantics mirror the Python contract
// (services/services/quota/src/quota/limiter.py::check_and_consume):
//
//   - Allowed=true:  TierBlocked=nil, Limit=nil, Remaining=&v (matches
//     Python L113-117, which sets only allowed + remaining + rule_source;
//     remaining = min(active max_count) - cost per Python
//     _remaining_for_first_active L230-239, or nil when no active tiers).
//   - Allowed=false: TierBlocked=&tier, Limit=&maxOfThatTier, Remaining=nil,
//     RetryAfterSeconds set (matches Python L135-141, which leaves remaining
//     None on the blocked path).
//
// Task 1 keeps the existing INCR+Expire per-tier logic and only threads
// the pointer fields through; Task 2 will swap the internals for an atomic
// Lua Eval (one RTT, no cross-tier race) and align rule_source values.
func (l *Limiter) CheckAndConsume(ctx context.Context, tenantID, appID, apiID string, rules *models.QuotaRules, cost int) *models.QuotaCheckResponse {
	resp := &models.QuotaCheckResponse{
		Allowed:           true,
		RuleSource:        "default",
		RetryAfterSeconds: 0,
	}

	now := time.Now()
	tiers := []struct {
		name string
		rule models.LimitRule
	}{
		{"second", rules.Second},
		{"minute", rules.Minute},
		{"day", rules.Day},
	}

	// Track the most-restrictive active limit for the allowed-path Remaining
	// (mirrors Python's _remaining_for_first_active: min of all active tier
	// maxes minus cost). This is computed across passing tiers; if any tier
	// blocks, we discard it and use the blocked-path shape instead.
	minActiveLimit := int64(-1)
	var firstBlocker struct {
		name      string
		limit     int64
		windowMs  int64
		key       string
	}

	for _, t := range tiers {
		if t.rule.MaxCount <= 0 {
			continue
		}
		limit := l.effectiveQuota(t.rule)
		if minActiveLimit < 0 || limit < minActiveLimit {
			minActiveLimit = limit
		}
		key := l.rateKey(tenantID, apiID, appID, tierChar(t.name), slotIndex(now, t.rule.WindowMs))

		count, err := l.redis.Incr(ctx, key).Result()
		if err != nil {
			// On Redis error, allow through but record nothing.
			continue
		}
		if count == 1 {
			l.redis.Expire(ctx, key, time.Duration(t.rule.WindowMs)*time.Millisecond)
		}

		if count > limit && resp.Allowed {
			// Blocked at this tier. Capture the first blocker's metadata; we
			// keep iterating so every tier still gets INCR'd (matching the
			// Python Lua script, which increments all three KEYS atomically).
			resp.Allowed = false
			firstBlocker.name = t.name
			firstBlocker.limit = limit
			firstBlocker.windowMs = t.rule.WindowMs
			firstBlocker.key = key
		}
	}

	if !resp.Allowed {
		// Blocked path (Python L135-141): tier_blocked + limit + retry_after_seconds,
		// remaining intentionally nil.
		tb := firstBlocker.name
		limit := int(firstBlocker.limit)
		resp.TierBlocked = &tb
		resp.Limit = &limit
		resp.Remaining = nil
		resp.RetryAfterSeconds = l.windowRemainingSeconds(ctx, firstBlocker.key, firstBlocker.windowMs)
		return resp
	}

	// Allowed path (Python L111-117): tier_blocked=nil, limit=nil, remaining set.
	resp.TierBlocked = nil
	resp.Limit = nil
	if minActiveLimit >= 0 {
		// Python _remaining_for_first_active: min(active max) - cost (>=0 clamp).
		rem := int(minActiveLimit) - cost
		if rem < 0 {
			rem = 0
		}
		resp.Remaining = &rem
	} else {
		resp.Remaining = nil
	}
	return resp
}

// windowRemainingSeconds returns the seconds remaining until the current
// window slot resets. It uses the Redis TTL (matching the Python Lua output)
// and falls back to the rule's window length on Redis error, clamped to >=1.
func (l *Limiter) windowRemainingSeconds(ctx context.Context, key string, windowMs int64) int {
	ttl, err := l.redis.TTL(ctx, key).Result()
	if err != nil || ttl <= 0 {
		fallback := int(windowMs / 1000)
		if fallback < 1 {
			fallback = 1
		}
		return fallback
	}
	secs := int(ttl.Seconds())
	if secs < 1 {
		secs = 1
	}
	return secs
}

// Refund decrements the counter for every tier by cost.
func (l *Limiter) Refund(ctx context.Context, tenantID, appID, apiID string, cost int) bool {
	now := time.Now()
	// tier -> windowMs; mirrors the Python REFUND script's three-key shape.
	tiers := []struct {
		tier string
		wms  int64
	}{
		{"s", 1000},
		{"m", 60000},
		{"d", 86400000},
	}

	for _, t := range tiers {
		key := l.rateKey(tenantID, apiID, appID, t.tier, slotIndex(now, t.wms))
		val, err := l.redis.DecrBy(ctx, key, int64(cost)).Result()
		if err != nil {
			continue
		}
		if val < 0 {
			l.redis.Set(ctx, key, 0, time.Duration(t.wms)*time.Millisecond)
		}
	}
	return true
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
		name           string
		rule           models.LimitRule
		canonicalSec   int64
		canonicalMs    int64
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
