package limiter

import (
	"testing"

	"github.com/alicebob/miniredis/v2"
	"github.com/redis/go-redis/v9"
)

func newTestLimiter(t *testing.T, splitEnv, multiActive string) *Limiter {
	t.Helper()
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	t.Setenv("QUOTA_REGION_SPLIT_RATIO", splitEnv)
	t.Setenv("MULTI_REGION_ACTIVE", multiActive)
	return New(rdb, "sh", 0) // 0 → fallback to env
}

func TestSplitRatioGuardedWhenSingleRegion(t *testing.T) {
	// MULTI_REGION_ACTIVE 未开 → splitRatio 必须 1.0，即便 env 设 0.6
	l := newTestLimiter(t, "0.6", "")
	if l.splitRatio != 1.0 {
		t.Fatalf("single-region splitRatio=%v, want 1.0", l.splitRatio)
	}
}

func TestSplitRatioAppliedWhenMultiRegion(t *testing.T) {
	l := newTestLimiter(t, "0.6", "1")
	if l.splitRatio != 0.6 {
		t.Fatalf("multi-region splitRatio=%v, want 0.6", l.splitRatio)
	}
}
