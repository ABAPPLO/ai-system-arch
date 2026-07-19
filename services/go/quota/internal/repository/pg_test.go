package repository

import (
	"testing"

	"github.com/abapplo/apihub/services/go/quota/internal/models"
)

// These tests pin Go's LoadRules merge/parse/source logic to Python's
// services/services/quota/src/quota/repository.py:49-114. The pure helpers
// (parseRulesBlob / mergeRules / rulesFromBlobs) are exercised directly so the
// full Python _merge + source contract is covered without a live PG
// dependency. The SQL wrapper around them (PGRepository.LoadRules) is verified
// end-to-end by the kind e2e in Task 2.

// activeTier is the expected shape of a parsed, active tier: Enabled=true,
// MaxCount set, Window = canonical tier window (1s/60s/86400s) when the blob
// doesn't override it (Python _parse_tier: `window = raw.get("window_seconds")
// or default_window`).
func activeTier(name string, max int, windowSec int64) models.LimitRule {
	return models.LimitRule{
		Tier:      name,
		MaxCount:  max,
		WindowSec: windowSec,
		WindowMs:  windowSec * 1000,
		Enabled:   true,
	}
}

// --- _parse_rules_blob port (Python repository.py:49-63) -------------------

// TestParseRulesBlob_FullShape: a blob with all three tiers parses to a fully
// populated QuotaRules, Enabled=true default, canonical windows when the blob
// omits window_seconds (Python _parse_tier L41: `window = raw.get(... ) or
// default_window`).
func TestParseRulesBlob_FullShape(t *testing.T) {
	blob := []byte(`{"second":{"max_count":10,"window_seconds":1},` +
		`"minute":{"max_count":100,"window_seconds":60},` +
		`"day":{"max_count":1000,"window_seconds":86400}}`)

	got := parseRulesBlob(blob)
	want := models.QuotaRules{
		Second: activeTier("second", 10, 1),
		Minute: activeTier("minute", 100, 60),
		Day:    activeTier("day", 1000, 86400),
	}
	if got != want {
		t.Fatalf("parseRulesBlob full shape:\n got %+v\nwant %+v", got, want)
	}
}

// TestParseRulesBlob_ShorthandMax: Python tolerates `max` / `count` aliases
// for `max_count` (Python _parse_tier L37:
// `raw.get("max_count") or raw.get("max") or raw.get("count")`).
func TestParseRulesBlob_ShorthandMax(t *testing.T) {
	cases := []struct {
		name string
		blob string
		max  int
	}{
		{"max_count", `{"second":{"max_count":7,"window_seconds":1}}`, 7},
		{"max shorthand", `{"second":{"max":7,"window_seconds":1}}`, 7},
		{"count shorthand", `{"second":{"count":7,"window_seconds":1}}`, 7},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := parseRulesBlob([]byte(c.blob))
			if got.Second.MaxCount != c.max {
				t.Fatalf("Second.MaxCount: got %d want %d", got.Second.MaxCount, c.max)
			}
			if !got.Second.Enabled {
				t.Fatalf("Second.Enabled: got false want true (Python default)")
			}
		})
	}
}

// TestParseRulesBlob_DefaultWindow: blob without window_seconds falls back to
// the canonical tier window (Python _parse_tier L41: `or default_window`).
func TestParseRulesBlob_DefaultWindow(t *testing.T) {
	blob := []byte(`{"second":{"max_count":5},"minute":{"max_count":50},"day":{"max_count":500}}`)
	got := parseRulesBlob(blob)
	if got.Second.WindowSec != 1 {
		t.Fatalf("Second.WindowSec: got %d want 1 (canonical)", got.Second.WindowSec)
	}
	if got.Minute.WindowSec != 60 {
		t.Fatalf("Minute.WindowSec: got %d want 60 (canonical)", got.Minute.WindowSec)
	}
	if got.Day.WindowSec != 86400 {
		t.Fatalf("Day.WindowSec: got %d want 86400 (canonical)", got.Day.WindowSec)
	}
}

// TestParseRulesBlob_BareIntShorthand: Python tolerates a bare int/float as
// the whole tier value, taking it as max_count with the default window
// (Python _parse_tier L30-32).
func TestParseRulesBlob_BareIntShorthand(t *testing.T) {
	blob := []byte(`{"second":42}`)
	got := parseRulesBlob(blob)
	want := activeTier("second", 42, 1)
	if got.Second != want {
		t.Fatalf("bare int shorthand:\n got %+v\nwant %+v", got.Second, want)
	}
}

// TestParseRulesBlob_DisabledExplicit: enabled=false survives the parse
// (Python _parse_tier L45: `enabled=raw.get("enabled", True)`). The parser
// must NOT silently force it back to true; the downstream limiter is the place
// that enforces "disabled = skip".
func TestParseRulesBlob_DisabledExplicit(t *testing.T) {
	blob := []byte(`{"second":{"max_count":5,"window_seconds":1,"enabled":false}}`)
	got := parseRulesBlob(blob)
	if got.Second.Enabled {
		t.Fatalf("Enabled: got true want false (parser must preserve explicit false)")
	}
	if got.Second.MaxCount != 5 {
		t.Fatalf("MaxCount: got %d want 5 (Enabled=false must not zero MaxCount)", got.Second.MaxCount)
	}
}

// TestParseRulesBlob_EnabledDefaultsTrue: omitting enabled in the blob
// defaults to true (Python _parse_tier L45 default).
func TestParseRulesBlob_EnabledDefaultsTrue(t *testing.T) {
	blob := []byte(`{"second":{"max_count":5,"window_seconds":1}}`)
	got := parseRulesBlob(blob)
	if !got.Second.Enabled {
		t.Fatalf("Enabled: got false want true (Python default when omitted)")
	}
}

// TestParseRulesBlob_NullEmpty: nil / empty / invalid JSON all yield the
// unlimited empty QuotaRules (Python _parse_rules_blob L51-57 returns
// EMPTY_RULES on json error or non-dict).
func TestParseRulesBlob_NullEmpty(t *testing.T) {
	for _, raw := range [][]byte{nil, []byte(``), []byte(`null`), []byte(`not json`)} {
		got := parseRulesBlob(raw)
		if got != (models.QuotaRules{}) {
			t.Fatalf("parseRulesBlob(%q): got %+v want empty QuotaRules", string(raw), got)
		}
	}
}

// TestParseRulesBlob_TierAbsent: a tier key absent in the blob leaves that
// tier at zero-value (MaxCount=0 → limiter will skip).
func TestParseRulesBlob_TierAbsent(t *testing.T) {
	blob := []byte(`{"second":{"max_count":5,"window_seconds":1}}`)
	got := parseRulesBlob(blob)
	if got.Second.MaxCount != 5 {
		t.Fatalf("Second.MaxCount: got %d want 5", got.Second.MaxCount)
	}
	if got.Minute.MaxCount != 0 {
		t.Fatalf("Minute.MaxCount: got %d want 0 (absent tier)", got.Minute.MaxCount)
	}
	if got.Day.MaxCount != 0 {
		t.Fatalf("Day.MaxCount: got %d want 0 (absent tier)", got.Day.MaxCount)
	}
}

// --- _merge port (Python repository.py:66-72) ------------------------------

// TestMerge_OverrideWinsPerTier: override wins tier-by-tier; base fills only
// the tiers override left empty (Python _merge L68-72:
// `override.second or base.second`).
func TestMerge_OverrideWinsPerTier(t *testing.T) {
	base := models.QuotaRules{
		Second: activeTier("second", 10, 1),
		Minute: activeTier("minute", 100, 60),
		Day:    activeTier("day", 1000, 86400),
	}
	override := models.QuotaRules{
		Second: activeTier("second", 20, 1), // overrides base.Second only
	}
	got := mergeRules(base, override)
	if got.Second.MaxCount != 20 {
		t.Fatalf("Second.MaxCount: got %d want 20 (override wins)", got.Second.MaxCount)
	}
	if got.Minute.MaxCount != 100 {
		t.Fatalf("Minute.MaxCount: got %d want 100 (base retained)", got.Minute.MaxCount)
	}
	if got.Day.MaxCount != 1000 {
		t.Fatalf("Day.MaxCount: got %d want 1000 (base retained)", got.Day.MaxCount)
	}
}

// TestMerge_BothEmpty: merging two empty QuotaRules yields empty (unlimited).
func TestMerge_BothEmpty(t *testing.T) {
	got := mergeRules(models.QuotaRules{}, models.QuotaRules{})
	if got != (models.QuotaRules{}) {
		t.Fatalf("merge empty+empty:\n got %+v\nwant empty", got)
	}
}

// --- rulesFromBlobs: full parse+merge+source port (Python L99-114) ---------

// TestRulesFromBlobs_OnlyApiVersion: scenario (a) — only api_version layer set
// → source="api_version", rule from api_version blob.
func TestRulesFromBlobs_OnlyApiVersion(t *testing.T) {
	apiRL := []byte(`{"second":{"max_count":10,"window_seconds":1},"minute":{"max_count":100,"window_seconds":60},"day":{"max_count":1000,"window_seconds":86400}}`)
	got, source := rulesFromBlobs(nil, nil, apiRL)
	if source != "api_version" {
		t.Fatalf("source: got %q want api_version", source)
	}
	if got.Second.MaxCount != 10 {
		t.Fatalf("Second.MaxCount: got %d want 10", got.Second.MaxCount)
	}
	if got.Minute.MaxCount != 100 {
		t.Fatalf("Minute.MaxCount: got %d want 100", got.Minute.MaxCount)
	}
	if got.Day.MaxCount != 1000 {
		t.Fatalf("Day.MaxCount: got %d want 1000", got.Day.MaxCount)
	}
}

// TestRulesFromBlobs_AppOverridesTenantOverridesApi: scenario (b) — app wins
// over tenant wins over api_version per-tier. Source="app" because app_rl is
// the highest-priority non-null layer.
func TestRulesFromBlobs_AppOverridesTenantOverridesApi(t *testing.T) {
	appRL := []byte(`{"second":{"max_count":20,"window_seconds":1}}`) // overrides second only
	tenantRL := []byte(`{"second":{"max_count":5,"window_seconds":1},"minute":{"max_count":50,"window_seconds":60}}`)
	apiRL := []byte(`{"second":{"max_count":10,"window_seconds":1},"minute":{"max_count":100,"window_seconds":60},"day":{"max_count":1000,"window_seconds":86400}}`)

	got, source := rulesFromBlobs(appRL, tenantRL, apiRL)
	if source != "app" {
		t.Fatalf("source: got %q want app", source)
	}
	// app second (20) wins over tenant (5) and api (10).
	if got.Second.MaxCount != 20 {
		t.Fatalf("Second.MaxCount: got %d want 20 (app wins)", got.Second.MaxCount)
	}
	// app doesn't set minute → tenant minute (50) wins over api (100).
	if got.Minute.MaxCount != 50 {
		t.Fatalf("Minute.MaxCount: got %d want 50 (tenant wins, app unset)", got.Minute.MaxCount)
	}
	// neither app nor tenant set day → api day (1000) is the fallback.
	if got.Day.MaxCount != 1000 {
		t.Fatalf("Day.MaxCount: got %d want 1000 (api_version fallback)", got.Day.MaxCount)
	}
}

// TestRulesFromBlobs_AllEmptyUnlimited: scenario (c) — all three layers NULL
// → QuotaRules{} (unlimited, mirrors Python EMPTY_RULES) and source="default".
func TestRulesFromBlobs_AllEmptyUnlimited(t *testing.T) {
	got, source := rulesFromBlobs(nil, nil, nil)
	if source != "default" {
		t.Fatalf("source: got %q want default", source)
	}
	if *got != (models.QuotaRules{}) {
		t.Fatalf("rules: got %+v want empty (unlimited)", *got)
	}
	// All tiers MaxCount=0 → limiter's hasActive short-circuit fires and the
	// response is rule_source="unlimited" (verified in limiter tests).
	if got.Second.MaxCount != 0 || got.Minute.MaxCount != 0 || got.Day.MaxCount != 0 {
		t.Fatalf("expected all MaxCount=0 (unlimited): %+v", *got)
	}
}

// TestRulesFromBlobs_DisabledTierHonored: scenario (d) — a tier with
// enabled=false at the override layer still wins the merge (override is
// truthy in Python), but the limiter downstream skips it because Enabled=false.
// Documents the contract: parseRulesBlob preserves Enabled=false, mergeRules
// passes it through, limiter is the place that enforces "disabled = skip".
func TestRulesFromBlobs_DisabledTierHonored(t *testing.T) {
	// app explicitly disables the second tier; api_version has it enabled.
	appRL := []byte(`{"second":{"max_count":99,"window_seconds":1,"enabled":false}}`)
	apiRL := []byte(`{"second":{"max_count":10,"window_seconds":1}}`)

	got, source := rulesFromBlobs(appRL, nil, apiRL)
	if source != "app" {
		t.Fatalf("source: got %q want app (app_rl non-null)", source)
	}
	// app override wins → MaxCount=99 but Enabled=false (downstream limiter
	// MUST skip this tier).
	if got.Second.MaxCount != 99 {
		t.Fatalf("Second.MaxCount: got %d want 99 (override wins merge)", got.Second.MaxCount)
	}
	if got.Second.Enabled {
		t.Fatalf("Second.Enabled: got true want false (parser must preserve explicit false)")
	}
}

// TestRulesFromBlobs_TenantOnly: tenant layer alone (no app, no api_version)
// → source="tenant".
func TestRulesFromBlobs_TenantOnly(t *testing.T) {
	tenantRL := []byte(`{"second":{"max_count":3,"window_seconds":1}}`)
	got, source := rulesFromBlobs(nil, tenantRL, nil)
	if source != "tenant" {
		t.Fatalf("source: got %q want tenant", source)
	}
	if got.Second.MaxCount != 3 {
		t.Fatalf("Second.MaxCount: got %d want 3", got.Second.MaxCount)
	}
}

// TestRulesFromBlobs_EmptyObjectFallsThrough: T1 review Minor #3 — an empty
// `{}` jsonb at the app layer must NOT report source="app". Python's
// `if row["app_rl"]:` truthiness check on the asyncpg-decoded dict treats
// `{}` as falsy and falls through to the next layer. Go's prior `appRL !=
// nil` check missed this (a non-nil `[]byte("{}")` reported source="app"
// even though the app layer contributed no rule — misleading for operators).
// Pin the faithful port: `{}` → fall through.
func TestRulesFromBlobs_EmptyObjectFallsThrough(t *testing.T) {
	// app=`{}` (empty object), tenant=NULL, api_version has real rules.
	// Source should be "api_version" (app falls through as if NULL).
	got, source := rulesFromBlobs([]byte(`{}`), nil, []byte(`{"second":{"max_count":7,"window_seconds":1}}`))
	if source != "api_version" {
		t.Fatalf("source: got %q want api_version (app `{}` must fall through)", source)
	}
	if got.Second.MaxCount != 7 {
		t.Fatalf("Second.MaxCount: got %d want 7 (from api_version)", got.Second.MaxCount)
	}

	// All three layers `{}` → source="default", EMPTY_RULES.
	got, source = rulesFromBlobs([]byte(`{}`), []byte(`{}`), []byte(`{}`))
	if source != "default" {
		t.Fatalf("source: got %q want default (all empty `{}`)", source)
	}
	if *got != (models.QuotaRules{}) {
		t.Fatalf("rules: got %+v want empty (all `{}` → unlimited)", *got)
	}
}

// TestIsTruthyBlob: pin the truthiness table to Python's `if row[col]:` on
// asyncpg-decoded jsonb. Empty collection / null / false-y JSON → false.
func TestIsTruthyBlob(t *testing.T) {
	cases := []struct {
		name string
		raw  []byte
		want bool
	}{
		{"nil (NULL column)", nil, false},
		{"empty bytes", []byte(""), false},
		{"json null", []byte("null"), false},
		{"empty object", []byte("{}"), false},
		{"empty array", []byte("[]"), false},
		{"false literal", []byte("false"), false},
		{"empty string", []byte(`""`), false},
		{"non-empty object", []byte(`{"second":1}`), true},
		{"non-empty array", []byte(`[1]`), true},
		{"number literal", []byte("42"), true},
		{"true literal", []byte("true"), true},
		{"invalid json", []byte("not json"), false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := isTruthyBlob(c.raw); got != c.want {
				t.Fatalf("isTruthyBlob(%q): got %v want %v", string(c.raw), got, c.want)
			}
		})
	}
}
