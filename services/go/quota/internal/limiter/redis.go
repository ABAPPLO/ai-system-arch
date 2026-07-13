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

// Limiter implements rate limiting with Redis sliding-window counters.
// Each tier (second / minute / day) uses a region-prefixed key:
//
//	t:{region}:rate:{tenant_id}:{api_id}:{app_id}:{tier}:{slot}
//
// where slot = now_ms / window_ms.
//
// The splitRatio field reduces effective quota for a non-primary region so
// that two regions sharing a single logical quota each get only their share.
type Limiter struct {
	redis      *redis.Client
	region     string
	splitRatio float64
}

// New creates a Limiter backed by the provided Redis client.
// An empty region falls back to the HOME_REGION env var.
// A zero or negative splitRatio falls back to QUOTA_REGION_SPLIT_RATIO
// (default 0.5).
func New(rdb *redis.Client, region string, splitRatio float64) *Limiter {
	if region == "" {
		region = os.Getenv("HOME_REGION")
	}
	if splitRatio <= 0 {
		splitRatio = parseFloat64(os.Getenv("QUOTA_REGION_SPLIT_RATIO"), 0.5)
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

// rateKey returns the region-prefixed Redis key for a rate-counter slot.
func (l *Limiter) rateKey(tenantID, apiID, appID, slot string) string {
	return fmt.Sprintf("t:%s:rate:%s:%s:%s:%s", l.region, tenantID, apiID, appID, slot)
}

// effectiveQuota applies the region split ratio to the rule's MaxCount.
// When two regions share one quota, each gets splitRatio * limit.
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
// the merged response. If any tier is exceeded the request is rejected.
func (l *Limiter) CheckAndConsume(ctx context.Context, tenantID, appID, apiID string, rules *models.QuotaRules, cost int) *models.QuotaCheckResponse {
	resp := &models.QuotaCheckResponse{
		Allowed:    true,
		RuleSource: "default",
	}

	now := time.Now()
	var minLimit int
	var minRemaining int
	var minResetMs int64

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
		key := l.rateKey(tenantID, apiID, appID, slotIndex(now, t.rule.WindowMs))

		count, err := l.redis.Incr(ctx, key).Result()
		if err != nil {
			// On Redis error, allow through but record nothing.
			continue
		}
		if count == 1 {
			l.redis.Expire(ctx, key, time.Duration(t.rule.WindowMs)*time.Millisecond)
		}

		remaining := int(limit) - int(count)
		if remaining < 0 {
			remaining = 0
		}

		if resp.Allowed && count > limit {
			resp.Allowed = false
			resp.TierBlocked = t.name
		}

		if minLimit == 0 || int(limit) < minLimit {
			minLimit = int(limit)
			minRemaining = remaining
			minResetMs = t.rule.WindowMs
		}
	}

	resp.Current = minLimit - minRemaining
	resp.Limit = minLimit
	resp.Remaining = minRemaining
	resp.ResetMs = minResetMs

	return resp
}

// Refund decrements the counter for every tier by cost.
func (l *Limiter) Refund(ctx context.Context, tenantID, appID, apiID string, cost int) bool {
	now := time.Now()
	windows := []int64{1000, 60000, 86400000} // second / minute / day

	for _, w := range windows {
		key := l.rateKey(tenantID, apiID, appID, slotIndex(now, w))
		val, err := l.redis.DecrBy(ctx, key, int64(cost)).Result()
		if err != nil {
			continue
		}
		if val < 0 {
			l.redis.Set(ctx, key, 0, time.Duration(w)*time.Millisecond)
		}
	}
	return true
}

// GetUsage returns current usage points for all configured tiers.
func (l *Limiter) GetUsage(ctx context.Context, tenantID, appID, apiID string, rules *models.QuotaRules) *models.UsageResponse {
	now := time.Now()
	points := make([]models.UsagePoint, 0, 3)

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
		key := l.rateKey(tenantID, apiID, appID, slotIndex(now, t.rule.WindowMs))

		count, err := l.redis.Get(ctx, key).Int()
		if err == redis.Nil {
			count = 0
		} else if err != nil {
			continue
		}

		remaining := int(limit) - count
		if remaining < 0 {
			remaining = 0
		}

		points = append(points, models.UsagePoint{
			Tier:      t.name,
			Used:      count,
			Limit:     int(limit),
			Remaining: remaining,
			ResetMs:   t.rule.WindowMs,
		})
	}

	return &models.UsageResponse{Points: points}
}
