package models

import (
	"encoding/json"
	"strings"
	"testing"
)

// TestQuotaCheckResponseJSONShape asserts that QuotaCheckResponse marshals to
// the exact key set Python's pydantic model emits, with snake_case tags and
// no leftover current/reset_ms fields (the pre-R3a shape).
func TestQuotaCheckResponseJSONShape(t *testing.T) {
	blockedTier := "second"
	blockedLimit := 10
	resp := QuotaCheckResponse{
		Allowed:           false,
		TierBlocked:       &blockedTier,
		Limit:             &blockedLimit,
		Remaining:         nil,
		RetryAfterSeconds: 1,
		RuleSource:        "rules",
	}
	data, err := json.Marshal(resp)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}

	wantKeys := []string{"allowed", "tier_blocked", "limit", "remaining", "retry_after_seconds", "rule_source"}
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(data, &raw); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if len(raw) != len(wantKeys) {
		t.Fatalf("key count mismatch: got %d (%v), want %d (%s)",
			len(raw), raw, len(wantKeys), string(data))
	}
	for _, k := range wantKeys {
		if _, ok := raw[k]; !ok {
			t.Fatalf("missing key %q in JSON: %s", k, string(data))
		}
	}
	for _, banned := range []string{"current", "reset_ms", "Current", "ResetMs"} {
		if _, ok := raw[banned]; ok {
			t.Fatalf("banned key %q present: %s", banned, string(data))
		}
	}

	// Field order must match Python's declaration order (allowed → rule_source).
	gotOrder := orderedKeys(data)
	if len(gotOrder) != len(wantKeys) {
		t.Fatalf("orderedKeys length mismatch: got %v want %v", gotOrder, wantKeys)
	}
	for i, k := range wantKeys {
		if gotOrder[i] != k {
			t.Fatalf("field order[%d]: got %q want %q (full: %s)", i, gotOrder[i], k, string(data))
		}
	}

	// retry_after_seconds must round-trip as a number.
	var ria struct {
		RetryAfterSeconds int `json:"retry_after_seconds"`
	}
	if err := json.Unmarshal(data, &ria); err != nil {
		t.Fatalf("unmarshal retry_after_seconds: %v", err)
	}
	if ria.RetryAfterSeconds != 1 {
		t.Fatalf("retry_after_seconds: got %d want 1", ria.RetryAfterSeconds)
	}

	// Blocked-shape: tier_blocked/limit as JSON literals, remaining as null.
	// Mirrors Python limiter.py:135-141 (blocked path leaves remaining=None).
	if got := string(raw["tier_blocked"]); got != `"second"` {
		t.Fatalf("tier_blocked: got %s want \"second\"", got)
	}
	if got := string(raw["limit"]); got != `10` {
		t.Fatalf("limit: got %s want 10", got)
	}
	if got := string(raw["remaining"]); got != `null` {
		t.Fatalf("remaining (blocked path): got %s want null", got)
	}
}

// TestQuotaCheckResponseJSONNullShape asserts the allowed-path shape emits
// tier_blocked / limit / remaining as JSON `null` when unset (not "" / 0),
// matching Python's pydantic Optional semantics — see
// services/services/quota/src/quota/models.py:42-54 and test_limiter.py:54
// (`assert resp.tier_blocked is None`). On the allowed path Python sets only
// remaining (via _remaining_for_first_active); when no active tiers exist,
// remaining is also None. This test exercises the all-nil case.
func TestQuotaCheckResponseJSONNullShape(t *testing.T) {
	resp := QuotaCheckResponse{
		Allowed:           true,
		TierBlocked:       nil,
		Limit:             nil,
		Remaining:         nil,
		RetryAfterSeconds: 0,
		RuleSource:        "default",
	}
	data, err := json.Marshal(resp)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(data, &raw); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	for _, k := range []string{"tier_blocked", "limit", "remaining"} {
		if got := string(raw[k]); got != `null` {
			t.Fatalf("%s: got %s want null (full: %s) — non-pointer emission breaks the Python contract", k, got, string(data))
		}
	}
	if got := string(raw["allowed"]); got != `true` {
		t.Fatalf("allowed: got %s want true", got)
	}

	// Allowed-with-remaining shape: tier_blocked/limit still null, remaining
	// carries the computed value (mirrors Python limiter.py:113-117).
	rem := 5
	resp2 := QuotaCheckResponse{
		Allowed:           true,
		TierBlocked:       nil,
		Limit:             nil,
		Remaining:         &rem,
		RetryAfterSeconds: 0,
		RuleSource:        "rules",
	}
	data2, _ := json.Marshal(resp2)
	var raw2 map[string]json.RawMessage
	json.Unmarshal(data2, &raw2)
	if got := string(raw2["tier_blocked"]); got != `null` {
		t.Fatalf("allowed.tier_blocked: got %s want null", got)
	}
	if got := string(raw2["limit"]); got != `null` {
		t.Fatalf("allowed.limit: got %s want null", got)
	}
	if got := string(raw2["remaining"]); got != `5` {
		t.Fatalf("allowed.remaining: got %s want 5", got)
	}
}

// TestUsageResponseJSONShape asserts the flat UsageResponse shape and the
// nullable limit pointer semantics (mirrors Python's `limit: int | None`).
func TestUsageResponseJSONShape(t *testing.T) {
	limit := 100
	resp := UsageResponse{
		TenantID: "tenant_a",
		AppID:    "app_y",
		APIID:    "api_x",
		Second:   UsagePoint{WindowSeconds: 1, Used: 5, Limit: &limit},
		Minute:   UsagePoint{WindowSeconds: 60, Used: 30, Limit: nil},
		Day:      UsagePoint{WindowSeconds: 86400, Used: 1000, Limit: &limit},
	}
	data, err := json.Marshal(resp)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}

	wantKeys := []string{"tenant_id", "app_id", "api_id", "second", "minute", "day"}
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(data, &raw); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if len(raw) != len(wantKeys) {
		t.Fatalf("key count mismatch: got %d (%v), want %d (%s)",
			len(raw), raw, len(wantKeys), string(data))
	}
	for _, k := range wantKeys {
		if _, ok := raw[k]; !ok {
			t.Fatalf("missing key %q in JSON: %s", k, string(data))
		}
	}
	for _, banned := range []string{"points", "Points", "tier", "reset_ms"} {
		if _, ok := raw[banned]; ok {
			t.Fatalf("banned key %q present: %s", banned, string(data))
		}
	}

	// Field order must match Python's declaration order.
	gotOrder := orderedKeys(data)
	for i, k := range wantKeys {
		if i >= len(gotOrder) || gotOrder[i] != k {
			t.Fatalf("field order[%d]: got %v want %q (full: %s)", i, gotOrder, k, string(data))
		}
	}

	// Sub-shape: each tier must be {window_seconds, used, limit} in order;
	// limit must serialize as null when nil and as a number when set.
	secondRaw := raw["second"]
	var secondMap map[string]json.RawMessage
	if err := json.Unmarshal(secondRaw, &secondMap); err != nil {
		t.Fatalf("unmarshal second: %v", err)
	}
	for _, k := range []string{"window_seconds", "used", "limit"} {
		if _, ok := secondMap[k]; !ok {
			t.Fatalf("second missing %q: %s", k, string(secondRaw))
		}
	}
	for _, banned := range []string{"tier", "remaining", "reset_ms"} {
		if _, ok := secondMap[banned]; ok {
			t.Fatalf("second has banned %q: %s", banned, string(secondRaw))
		}
	}
	if string(secondMap["limit"]) != "100" {
		t.Fatalf("second.limit: got %s want 100", string(secondMap["limit"]))
	}
	if string(raw["minute"]) == "" {
		t.Fatal("minute missing")
	}
	var minMap map[string]json.RawMessage
	json.Unmarshal(raw["minute"], &minMap)
	if string(minMap["limit"]) != "null" {
		t.Fatalf("minute.limit (nil): got %s want null", string(minMap["limit"]))
	}
}

// orderedKeys extracts JSON object keys in their serialized (declaration) order.
// Works only for flat objects.
func orderedKeys(data []byte) []string {
	dec := json.NewDecoder(strings.NewReader(string(data)))
	tok, err := dec.Token()
	if err != nil {
		return nil
	}
	delim, ok := tok.(json.Delim)
	if !ok || delim != '{' {
		return nil
	}
	var keys []string
	for dec.More() {
		t, err := dec.Token()
		if err != nil {
			return nil
		}
		key, ok := t.(string)
		if !ok {
			return nil
		}
		keys = append(keys, key)
		// Skip the value token (handle nested objects/arrays by depth).
		if err := skipValue(dec); err != nil {
			return nil
		}
	}
	return keys
}

func skipValue(dec *json.Decoder) error {
	t, err := dec.Token()
	if err != nil {
		return err
	}
	if _, ok := t.(json.Delim); !ok {
		// Scalar — token already consumed.
		return nil
	}
	// Nested object or array — drain until matching closer.
	depth := 1
	for depth > 0 {
		tt, err := dec.Token()
		if err != nil {
			return err
		}
		if dd, ok := tt.(json.Delim); ok {
			if dd == '{' || dd == '[' {
				depth++
			} else {
				depth--
			}
		}
	}
	return nil
}
