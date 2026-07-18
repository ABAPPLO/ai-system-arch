package limiter

import (
	"strings"
	"testing"
)

// TestRateKeyFormat verifies the Redis key shape aligns with the Python
// contract: t:{tenant}:rate:{api}:{app}:{tier}:{slot} — NO region prefix,
// tier is the single-char s/m/d tag.
func TestRateKeyFormat(t *testing.T) {
	l := &Limiter{} // rateKey ignores region/splitRatio in R3a

	cases := []struct {
		name     string
		tenant   string
		api      string
		app      string
		tier     string
		slot     string
		want     string
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
