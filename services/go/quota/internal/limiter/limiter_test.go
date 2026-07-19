package limiter

import (
	"context"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	"github.com/redis/go-redis/v9"

	"github.com/abapplo/apihub/services/go/quota/internal/models"
)

// TestRateKeyFormat verifies the Redis key shape aligns with the Python
// contract: t:{tenant}:rate:{api}:{app}:{tier}:{slot} — NO region prefix,
// tier is the single-char s/m/d tag.
func TestRateKeyFormat(t *testing.T) {
	l := &Limiter{} // rateKey ignores region/splitRatio in R3a

	cases := []struct {
		name   string
		tenant string
		api    string
		app    string
		tier   string
		slot   string
		want   string
	}{
		{
			name:   "second tier",
			tenant: "tenant_a", api: "api_x", app: "app_y", tier: "s", slot: "1700000000",
			want: "t:tenant_a:rate:api_x:app_y:s:1700000000",
		},
		{
			name:   "minute tier",
			tenant: "tenant_a", api: "api_x", app: "app_y", tier: "m", slot: "28333333",
			want: "t:tenant_a:rate:api_x:app_y:m:28333333",
		},
		{
			name:   "day tier",
			tenant: "tenant_a", api: "api_x", app: "app_y", tier: "d", slot: "19675",
			want: "t:tenant_a:rate:api_x:app_y:d:19675",
		},
	}

	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := l.rateKey(c.tenant, c.api, c.app, c.tier, c.slot)
			if got != c.want {
				t.Fatalf("rateKey(%s,%s,%s,%s,%s):\n got %q\nwant %q",
					c.tenant, c.api, c.app, c.tier, c.slot, got, c.want)
			}
			// Hard guarantee: no region segment, exactly 7 colon-separated
			// components (t:<tenant>:rate:<api>:<app>:<tier>:<slot>), tier ∈ {s,m,d}.
			if strings.HasPrefix(got, "t:"+l.region+":") && l.region != "" {
				t.Fatalf("key must not include region prefix: %q", got)
			}
			parts := strings.Split(got, ":")
			if len(parts) != 7 {
				t.Fatalf("expected 7 colon-separated segments, got %d: %q", len(parts), got)
			}
			if parts[0] != "t" {
				t.Fatalf("first segment must be 't': %q", got)
			}
			if parts[2] != "rate" {
				t.Fatalf("third segment must be 'rate' literal: %q", got)
			}
			switch parts[5] {
			case "s", "m", "d":
			default:
				t.Fatalf("tier segment must be s/m/d, got %q: %q", parts[5], got)
			}
		})
	}
}

// TestRateKeyNoRegion Leaks nothing via the receiver's region field — even
// when region is set, the key omits it (R3a contract).
func TestRateKeyNoRegion(t *testing.T) {
	l := &Limiter{region: "cn-beijing", splitRatio: 1.0}
	got := l.rateKey("tenant_a", "api_x", "app_y", "s", "123")
	if strings.Contains(got, "cn-beijing") {
		t.Fatalf("key must not embed region, got %q", got)
	}
	if got != "t:tenant_a:rate:api_x:app_y:s:123" {
		t.Fatalf("unexpected key: %q", got)
	}
}

// TestTierChar covers the name→char helper used to build keys.
func TestTierChar(t *testing.T) {
	cases := map[string]string{
		"second": "s",
		"minute": "m",
		"day":    "d",
	}
	for name, want := range cases {
		if got := tierChar(name); got != want {
			t.Fatalf("tierChar(%q): got %q want %q", name, got, want)
		}
	}
}

// --- Lua atomicity tests (Task 2) ------------------------------------------
//
// The pre-R3a limiter used per-key INCR + Expire across the three tiers. That
// has a race window: between the INCR and the limit check, multiple callers
// can observe an under-limit count and all admit. The Lua CHECK_AND_INCR
// script closes that window (Redis runs Lua atomically). These tests pin the
// contract: under tight limits and high concurrency, the admitted / blocked
// counts must be EXACT — no over-admit, no under-admit.
//
// We use miniredis (in-process Redis with gopher-lua) so the test has no
// external dependency and the race detector (`go test -race`) can observe any
// Go-side data race introduced by the rewrite.

// newMiniLimiter stands up a miniredis-backed Limiter for one test.
// miniredis.RunT handles teardown via t.Cleanup.
func newMiniLimiter(t *testing.T) (*Limiter, *miniredis.Miniredis, *redis.Client) {
	t.Helper()
	s := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: s.Addr()})
	t.Cleanup(func() { _ = rdb.Close() })
	return New(rdb, "", 1.0), s, rdb
}

// TestCheckAndConsumeLuaAtomicity hammers a tight second-tier limit from many
// goroutines and asserts the admitted count is EXACTLY the limit (no
// over-admit, which the pre-Lua INCR+Expire implementation would do under
// contention) and the blocked count is the remainder.
func TestCheckAndConsumeLuaAtomicity(t *testing.T) {
	l, _, _ := newMiniLimiter(t)

	const limit = 10
	const goroutines = 200
	rules := &models.QuotaRules{
		Second: models.LimitRule{Tier: "second", MaxCount: limit, WindowMs: 1000, Enabled: true},
		// minute / day MaxCount = 0 → Lua skips their check but still INCRs
		// so GET /usage reflects every attempt (Python _compile_rules: max=0
		// for inactive tiers).
	}

	var allowed, blocked int64
	var wg sync.WaitGroup
	start := make(chan struct{})
	wg.Add(goroutines)
	for i := 0; i < goroutines; i++ {
		go func() {
			defer wg.Done()
			<-start
			resp := l.CheckAndConsume(context.Background(),
				"tenant_a", "app_y", "api_x", rules, 1)
			if resp.Allowed {
				atomic.AddInt64(&allowed, 1)
			} else {
				atomic.AddInt64(&blocked, 1)
			}
		}()
	}
	close(start)
	wg.Wait()

	if allowed != limit {
		t.Fatalf("Lua over-admit or under-admit: allowed=%d want exactly %d (blocked=%d)",
			allowed, limit, blocked)
	}
	if blocked != goroutines-limit {
		t.Fatalf("blocked count: got %d want %d", blocked, goroutines-limit)
	}
	if got := allowed + blocked; got != goroutines {
		t.Fatalf("total responses: got %d want %d", got, goroutines)
	}
}

// TestCheckAndConsumeLuaAtomicityMultiTier runs the same concurrency stress
// against ALL three tiers with tight limits. The first goroutine wave blows
// through the second tier; later goroutines must report tier_blocked="second"
// (the strictest tier), never minute/day. Pins the 1-based tier_idx mapping
// in the Lua return tuple → Go's tiers[idx-1].
func TestCheckAndConsumeLuaAtomicityMultiTier(t *testing.T) {
	l, _, _ := newMiniLimiter(t)

	rules := &models.QuotaRules{
		Second: models.LimitRule{Tier: "second", MaxCount: 5, WindowMs: 1000, Enabled: true},
		Minute: models.LimitRule{Tier: "minute", MaxCount: 50, WindowMs: 60000, Enabled: true},
		Day:    models.LimitRule{Tier: "day", MaxCount: 5000, WindowMs: 86400000, Enabled: true},
	}

	const goroutines = 200
	var (
		allowed int64
		blocked map[string]int64
	)
	blocked = make(map[string]int64, 3)
	var mu sync.Mutex
	var wg sync.WaitGroup
	start := make(chan struct{})
	wg.Add(goroutines)
	for i := 0; i < goroutines; i++ {
		go func() {
			defer wg.Done()
			<-start
			resp := l.CheckAndConsume(context.Background(),
				"tenant_a", "app_y", "api_x", rules, 1)
			if resp.Allowed {
				atomic.AddInt64(&allowed, 1)
				return
			}
			if resp.TierBlocked == nil {
				t.Errorf("blocked response with nil tier_blocked: %+v", resp)
				return
			}
			mu.Lock()
			blocked[*resp.TierBlocked]++
			mu.Unlock()
		}()
	}
	close(start)
	wg.Wait()

	if allowed != 5 {
		t.Fatalf("allowed: got %d want 5 (second-tier limit)", allowed)
	}
	if got := blocked["second"]; got != goroutines-5 {
		t.Fatalf("blocked[second]: got %d want %d", got, goroutines-5)
	}
	for _, tier := range []string{"minute", "day"} {
		if got := blocked[tier]; got != 0 {
			t.Fatalf("blocked[%s]: got %d want 0 (second tier is stricter)", tier, got)
		}
	}
}

// TestCheckAndConsumeSequential verifies the sequential, single-goroutine
// contract: exactly `limit` calls pass, then the next call is blocked with
// tier_blocked + limit + retry_after_seconds set, remaining nil, and the
// rule_source="rules" on both branches (mirrors Python limiter.py:111-141).
func TestCheckAndConsumeSequential(t *testing.T) {
	l, _, _ := newMiniLimiter(t)

	rules := &models.QuotaRules{
		Second: models.LimitRule{Tier: "second", MaxCount: 3, WindowMs: 1000, Enabled: true},
	}
	const tenant, app, api = "t_seq", "a_seq", "b_seq"

	for i := 0; i < 3; i++ {
		resp := l.CheckAndConsume(context.Background(), tenant, app, api, rules, 1)
		if !resp.Allowed {
			t.Fatalf("call %d: expected allowed, got blocked at tier=%v limit=%v",
				i, resp.TierBlocked, resp.Limit)
		}
		if resp.RuleSource != "rules" {
			t.Fatalf("call %d rule_source: got %q want %q", i, resp.RuleSource, "rules")
		}
		if resp.Remaining == nil {
			t.Fatalf("call %d: remaining nil on allowed path", i)
		}
		// min active max = 3, cost = 1 → remaining = 3-1 = 2 every call
		// (Python _remaining_for_first_active returns min(active_max)-cost,
		// not max-current — see limiter.py:230-239).
		if *resp.Remaining != 2 {
			t.Fatalf("call %d remaining: got %d want 2", i, *resp.Remaining)
		}
		if resp.TierBlocked != nil || resp.Limit != nil {
			t.Fatalf("call %d: tier_blocked/limit must be nil on allowed path: %+v", i, resp)
		}
		if resp.RetryAfterSeconds != 0 {
			t.Fatalf("call %d retry_after_seconds: got %d want 0 on allowed", i, resp.RetryAfterSeconds)
		}
	}

	// 4th call must be blocked.
	resp := l.CheckAndConsume(context.Background(), tenant, app, api, rules, 1)
	if resp.Allowed {
		t.Fatal("4th call: expected blocked, got allowed")
	}
	if resp.RuleSource != "rules" {
		t.Fatalf("blocked rule_source: got %q want rules", resp.RuleSource)
	}
	if resp.TierBlocked == nil || *resp.TierBlocked != "second" {
		t.Fatalf("tier_blocked: got %v want second", resp.TierBlocked)
	}
	if resp.Limit == nil || *resp.Limit != 3 {
		t.Fatalf("limit: got %v want 3", resp.Limit)
	}
	if resp.Remaining != nil {
		t.Fatalf("remaining: got %v want nil on blocked path", *resp.Remaining)
	}
	// Second-tier TTL = 1s, so retry_after_seconds must be 1 (Lua TTL of the
	// blocked key right after EXPIRE).
	if resp.RetryAfterSeconds != 1 {
		t.Fatalf("retry_after_seconds: got %d want 1", resp.RetryAfterSeconds)
	}
}

// TestRuleSourceUnlimited verifies that an API with no configured limit never
// touches Redis (Python limiter.py:79-83 short-circuit) and reports
// rule_source="unlimited".
func TestRuleSourceUnlimited(t *testing.T) {
	l, mr, _ := newMiniLimiter(t)

	rules := &models.QuotaRules{} // all MaxCount = 0 → unlimited
	resp := l.CheckAndConsume(context.Background(), "t", "a", "b", rules, 1)
	if !resp.Allowed {
		t.Fatalf("expected allowed, got blocked: %+v", resp)
	}
	if resp.RuleSource != "unlimited" {
		t.Fatalf("rule_source: got %q want %q", resp.RuleSource, "unlimited")
	}
	// Must NOT have touched Redis — the unlimited path skips Eval entirely.
	if got := len(mr.Keys()); got != 0 {
		t.Fatalf("unlimited path touched Redis: keys=%d", got)
	}
}

// TestDisabledTierSkipped verifies the new Enabled-flag contract: a tier with
// Enabled=false (even with MaxCount>0) is treated as inactive — the limiter
// reports rule_source="unlimited" and never blocks. Mirrors Python
// _compile_rules (limiter.py:220: `if rule and rule.enabled and
// rule.max_count > 0`) which drops disabled tiers from active_tiers entirely.
func TestDisabledTierSkipped(t *testing.T) {
	l, mr, _ := newMiniLimiter(t)

	// All tiers Enabled=false → effectively unlimited, even though MaxCount>0.
	rules := &models.QuotaRules{
		Second: models.LimitRule{Tier: "second", MaxCount: 1, WindowMs: 1000, Enabled: false},
		Minute: models.LimitRule{Tier: "minute", MaxCount: 1, WindowMs: 60000, Enabled: false},
		Day:    models.LimitRule{Tier: "day", MaxCount: 1, WindowMs: 86400000, Enabled: false},
	}
	resp := l.CheckAndConsume(context.Background(), "t_dis", "a_dis", "b_dis", rules, 1)
	if !resp.Allowed {
		t.Fatalf("disabled tiers must not block: %+v", resp)
	}
	if resp.RuleSource != "unlimited" {
		t.Fatalf("rule_source: got %q want unlimited (all tiers disabled)", resp.RuleSource)
	}
	if got := len(mr.Keys()); got != 0 {
		t.Fatalf("all-disabled path touched Redis: keys=%d", got)
	}

	// Mixed: one enabled tier limits; disabled tiers do not contribute.
	rules2 := &models.QuotaRules{
		Second: models.LimitRule{Tier: "second", MaxCount: 2, WindowMs: 1000, Enabled: true},
		// minute disabled — would otherwise be the strictest tier.
		Minute: models.LimitRule{Tier: "minute", MaxCount: 1, WindowMs: 60000, Enabled: false},
	}
	// Two calls must admit against the second-tier limit=2; the disabled
	// minute=1 must NOT block.
	for i := 0; i < 2; i++ {
		resp := l.CheckAndConsume(context.Background(), "t_mix", "a_mix", "b_mix", rules2, 1)
		if !resp.Allowed {
			t.Fatalf("call %d: disabled minute must not block: %+v", i, resp)
		}
	}
	// 3rd call exceeds the second-tier limit=2 → blocked at second, NOT minute.
	resp3 := l.CheckAndConsume(context.Background(), "t_mix", "a_mix", "b_mix", rules2, 1)
	if resp3.Allowed {
		t.Fatal("3rd call: expected blocked at second, got allowed")
	}
	if resp3.TierBlocked == nil || *resp3.TierBlocked != "second" {
		t.Fatalf("tier_blocked: got %v want second (minute is disabled)", resp3.TierBlocked)
	}
}

// TestRuleSourceFallback verifies Redis errors degrade to allowed +
// rule_source="fallback" (Python limiter.py:97-108: a downed limiter should
// not block all traffic).
func TestRuleSourceFallback(t *testing.T) {
	// Stand up miniredis, then close it so every Eval hits a broken
	// connection. MaxRetries=1 keeps the test fast (default 3 × 512ms backoff
	// per attempt × 5 dial attempts each → ~1.7s; we want the failure mode,
	// not the wait).
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr(), MaxRetries: 1})
	t.Cleanup(func() { _ = rdb.Close() })
	l := New(rdb, "", 1.0)
	mr.Close()

	rules := &models.QuotaRules{Second: models.LimitRule{MaxCount: 5, WindowMs: 1000, Enabled: true}}
	resp := l.CheckAndConsume(context.Background(), "t", "a", "b", rules, 1)
	if !resp.Allowed {
		t.Fatalf("fallback must allow: %+v", resp)
	}
	if resp.RuleSource != "fallback" {
		t.Fatalf("rule_source: got %q want fallback", resp.RuleSource)
	}
	if resp.RetryAfterSeconds != 0 {
		t.Fatalf("retry_after_seconds on fallback: got %d want 0", resp.RetryAfterSeconds)
	}
}

// TestRefundAtomic verifies the Lua REFUND script rewinds the counter exactly
// (atomic DecrBy across tiers in one Eval) so subsequent CheckAndConsume calls
// against a tight limit re-admit the refunded budget.
func TestRefundAtomic(t *testing.T) {
	l, _, _ := newMiniLimiter(t)

	rules := &models.QuotaRules{Second: models.LimitRule{MaxCount: 5, WindowMs: 1000, Enabled: true}}
	const tenant, app, api = "t_rf", "a_rf", "b_rf"

	// Consume 3.
	for i := 0; i < 3; i++ {
		resp := l.CheckAndConsume(context.Background(), tenant, app, api, rules, 1)
		if !resp.Allowed {
			t.Fatalf("setup consume %d: %v", i, resp)
		}
	}

	// Refund 2 — should leave 1 used, 4 remaining.
	if ok := l.Refund(context.Background(), tenant, app, api, 2); !ok {
		t.Fatal("refund returned false")
	}

	// Now 4 more must admit (5 limit - 1 used), the 5th must block.
	allowed := 0
	for i := 0; i < 5; i++ {
		resp := l.CheckAndConsume(context.Background(), tenant, app, api, rules, 1)
		if resp.Allowed {
			allowed++
		}
	}
	if allowed != 4 {
		t.Fatalf("after refund of 2: got %d re-admitted want 4 (refund didn't rewind)", allowed)
	}
}

// TestRefundDoesNotGoNegative verifies the Lua script clamps the counter at 0
// when refunding more than was consumed (Python lua_scripts.py:58-60 —
// `if new_val < 0 then new_val = 0 end`). Without the clamp, a subsequent
// check-against-limit=1 would underflow and incorrectly block.
func TestRefundDoesNotGoNegative(t *testing.T) {
	l, _, _ := newMiniLimiter(t)

	const tenant, app, api = "t_neg", "a_neg", "b_neg"
	// Refund 100 without any prior consume — script must clamp to 0.
	if ok := l.Refund(context.Background(), tenant, app, api, 100); !ok {
		t.Fatal("refund returned false")
	}
	// First real consume against a limit of 1 must admit (counter is 0).
	rules := &models.QuotaRules{Second: models.LimitRule{MaxCount: 1, WindowMs: 1000, Enabled: true}}
	resp := l.CheckAndConsume(context.Background(), tenant, app, api, rules, 1)
	if !resp.Allowed {
		t.Fatalf("after over-refund: expected allowed (clamp to 0 broken): %+v", resp)
	}
	// Second consume must block at the second tier.
	resp2 := l.CheckAndConsume(context.Background(), tenant, app, api, rules, 1)
	if resp2.Allowed {
		t.Fatal("second consume: expected blocked, got allowed")
	}
	if resp2.TierBlocked == nil || *resp2.TierBlocked != "second" {
		t.Fatalf("tier_blocked: got %v want second", resp2.TierBlocked)
	}
}

// TestRefundOnRedisError verifies Refund returns false on Redis failure
// (best-effort — Python limiter.py:159-166 logs and returns False, never
// raising). MaxRetries=1 keeps the test fast.
func TestRefundOnRedisError(t *testing.T) {
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr(), MaxRetries: 1})
	t.Cleanup(func() { _ = rdb.Close() })
	l := New(rdb, "", 1.0)
	mr.Close()

	if ok := l.Refund(context.Background(), "t", "a", "b", 1); ok {
		t.Fatal("refund on broken Redis: got true want false")
	}
}

// TestRateKeysForTiersOrder pins the KEYS-order contract: the slice returned
// must be [second, minute, day] in exactly that order, because the Lua
// CHECK_AND_INCR script indexes KEYS[1..n] by tier_idx (1=second, 2=minute,
// 3=day) and returns the 1-based blocking tier. Swapping the order would
// silently misattribute TierBlocked.
func TestRateKeysForTiersOrder(t *testing.T) {
	l := &Limiter{}
	keys := l.rateKeysForTiers("tenant_a", "api_x", "app_y", time.Unix(1700000000, 0))
	if len(keys) != 3 {
		t.Fatalf("len: got %d want 3", len(keys))
	}
	wantS := "t:tenant_a:rate:api_x:app_y:s:1700000000"
	wantM := "t:tenant_a:rate:api_x:app_y:m:28333333" // 1700000000 / 60
	wantD := "t:tenant_a:rate:api_x:app_y:d:19675"    // 1700000000 / 86400
	if keys[0] != wantS {
		t.Fatalf("keys[0]: got %q want %q (must be second)", keys[0], wantS)
	}
	if keys[1] != wantM {
		t.Fatalf("keys[1]: got %q want %q (must be minute)", keys[1], wantM)
	}
	if keys[2] != wantD {
		t.Fatalf("keys[2]: got %q want %q (must be day)", keys[2], wantD)
	}
	// Cross-check the values against Python's _rate_keys for ts=1700000000:
	// second_slot=1700000000, minute_slot=28333333, day_slot=19675.
}
