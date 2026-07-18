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
// the merged response. Field semantics mirror the Python contract:
//   - Allowed=true:  Limit/Remaining = most-restrictive active tier's values,
//     RetryAfterSeconds=0.
//   - Allowed=false: TierBlocked/Limit/RetryAfterSeconds describe the first
//     tier that blocked; Remaining=0.
//
// Task 1 keeps the existing INCR+Expire per-tier logic and only threads
// RetryAfterSeconds through; Task 2 will swap the internals for an atomic
// Lua Eval (one RTT, no cross-tier race).
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

	for _, t := range tiers {
		if t.rule.MaxCount <= 0 {
			continue
		}
		limit := l.effectiveQuota(t.rule)
		key := l.rateKey(tenantID, apiID, appID, tierChar(t.name), slotIndex(now, t.rule.WindowMs))

		count, err := l.redis.Incr(ctx, key).Result()
		if err != nil {
			// On Redis error, allow through but record nothing.
			continue
		}
		if count == 1 {
			l.redis.Expire(ctx, key, time.Duration(t.rule.WindowMs)*time.Millisecond)
		}

		if count > limit {
			// Blocked at this tier. Capture the first blocker's metadata.
			if resp.Allowed {
				resp.Allowed = false
				resp.TierBlocked = t.name
				resp.Limit = int(limit)
				resp.Remaining = 0
				resp.RetryAfterSeconds = l.windowRemainingSeconds(ctx, key, t.rule.WindowMs)
			}
			continue
		}

		// Tier passed. Track the most-restrictive active limit for the
		// allowed-path response (matches Python's _remaining_for_first_active).
		remaining := int(limit) - int(count)
		if remaining < 0 {
			remaining = 0
		}
		if resp.Limit == 0 || int(limit) < resp.Limit {
			resp.Limit = int(limit)
			resp.Remaining = remaining
		}
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
func (l *Limiter) GetUsage(ctx context.Context, tenantID, appID, apiID string, rules *models.QuotaRules) *models.UsageResponse {
	now := time.Now()
	resp := &models.UsageResponse{
		TenantID: tenantID,
		AppID:    appID,
		APIID:    apiID,
	}

	tiers := []struct {
		name string
		rule models.LimitRule
	}{
		{"second", rules.Second},
		{"minute", rules.Minute},
		{"day", rules.Day},
	}

	for _, t := range tiers {
		point := models.UsagePoint{
			WindowSeconds: t.rule.WindowSec,
			Used:          0,
		}
		if point.WindowSeconds <= 0 {
			// Rule unconfigured → fall back to canonical window for the tier.
			switch t.name {
			case "second":
				point.WindowSeconds = 1
			case "minute":
				point.WindowSeconds = 60
			case "day":
				point.WindowSeconds = 86400
			}
		}
		windowMs := point.WindowSeconds * 1000

		if t.rule.MaxCount > 0 {
			limit := int(l.effectiveQuota(t.rule))
			point.Limit = &limit
		}

		key := l.rateKey(tenantID, apiID, appID, tierChar(t.name), slotIndex(now, windowMs))
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
