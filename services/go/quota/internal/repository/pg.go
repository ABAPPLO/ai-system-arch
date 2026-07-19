package repository

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/abapplo/apihub/services/go/quota/internal/models"
)

type PGRepository struct {
	pool *pgxpool.Pool
}

// NewPGRepository builds the pgxpool used by LoadRules. The pool connects as
// the business user (PG_USER=apihub_app, NOSUPERUSER NOBYPASSRLS — see
// scripts/init-db/00-roles.sql), which is subject to ROW LEVEL SECURITY on
// app / api_version (FORCE ROW LEVEL SECURITY; owner-only bypass doesn't help
// because the business user is NOT the owner).
//
// Python's load_rules wraps its query in db.admin_db_session(), which runs
// `SELECT set_config('app.is_platform_admin', 'true', true)` per transaction
// to bypass RLS (services/libs/apihub-core/src/apihub_core/db.py:215-247).
// Quota is a platform service that legitimately needs cross-tenant visibility
// on rate_limit metadata — same rationale as auth/api-registry. We set the
// same GUC session-wide on every new pool connection via AfterConnect so
// LoadRules sees all tenants' app/api_version rows regardless of caller.
//
// Surfaced by Task 2 seed (13-quota-rules-seed.sql): without this hook, RLS
// silently hides every app/api_version row → all three subqueries return NULL
// → LoadRules reports ("default", EMPTY_RULES) even with seeded data, and
// the main e2e path stays unlimited.
func NewPGRepository(ctx context.Context, dsn string, poolSize int) (*PGRepository, error) {
	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		return nil, fmt.Errorf("parse dsn: %w", err)
	}
	cfg.MaxConns = int32(poolSize)
	cfg.AfterConnect = func(ctx context.Context, conn *pgx.Conn) error {
		// is_local=false → session-level (persists for the life of this
		// pooled connection, so subsequent QueryRow calls inherit it without
		// re-issuing). Matches Python admin_db_session's set_config(..., true)
		// semantics except Python's `true` is transaction-local (admin_db_session
		// runs inside an explicit tr.start()/commit()); we're not in a tx here.
		if _, err := conn.Exec(ctx,
			"SELECT set_config('app.is_platform_admin', 'true', false)"); err != nil {
			return fmt.Errorf("set platform_admin: %w", err)
		}
		return nil
	}
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		return nil, fmt.Errorf("create pool: %w", err)
	}
	if err := pool.Ping(ctx); err != nil {
		return nil, fmt.Errorf("ping: %w", err)
	}
	return &PGRepository{pool: pool}, nil
}

func (r *PGRepository) Close() {
	r.pool.Close()
}

// Canonical tier windows in SECONDS — matches Python TIER_SECOND / TIER_MINUTE
// / TIER_DAY (services/services/quota/src/quota/models.py:6-8). Used as the
// default window_seconds when a parsed tier blob omits window_seconds (Python
// _parse_tier L41: `window = raw.get("window_seconds") or default_window`).
const (
	tierSecondSecs int64 = 1
	tierMinuteSecs int64 = 60
	tierDaySecs    int64 = 86400
)

// loadRulesSQL is a verbatim port of Python repository.load_rules' query
// (services/services/quota/src/quota/repository.py:82-92). Param order is
// (app_id, tenant_id, api_id) — same as Python (note Python passes them as
// app_id, tenant_id, api_id after the SQL string in conn.fetch).
//
// Each scalar subquery returns the rate_limit JSONB of one layer (or NULL when
// the row doesn't exist). The outer SELECT always yields exactly one row
// (scalar subqueries), so QueryRow().Scan is correct.
const loadRulesSQL = `
SELECT
    (SELECT rate_limit FROM app WHERE id = $1 AND tenant_id = $2)     AS app_rl,
    (SELECT rate_limit FROM tenant WHERE id = $2)                     AS tenant_rl,
    (SELECT rate_limit FROM api_version
        WHERE api_id = $3 AND tenant_id = $2
        ORDER BY status = 'published' DESC, created_at DESC LIMIT 1) AS api_rl
`

// LoadRules aligns with Python load_rules (repository.py:75-114). Queries the
// three rate_limit JSONB layers (app / tenant / api_version) in one SQL
// roundtrip, parses + merges them, and returns (merged, source) where source
// is the highest-priority non-null layer ("app" > "tenant" > "api_version" >
// "default").
//
// All-empty → *QuotaRules{} (MaxCount=0 everywhere → limiter treats as
// unlimited, mirrors Python EMPTY_RULES).
//
// On any PG error the function degrades to (EMPTY_RULES, "default", nil)
// rather than 500'ing the request path — quota rule loading must never break
// business traffic. (Python returns EMPTY_RULES when `if not rows`; we extend
// the same defensive semantics to PG-level errors.)
func (r *PGRepository) LoadRules(ctx context.Context, tenantID, appID, apiID string) (*models.QuotaRules, string, error) {
	var appRL, tenantRL, apiRL []byte
	err := r.pool.QueryRow(ctx, loadRulesSQL,
		appID, tenantID, apiID, // same param order as Python repository.py:90-93
	).Scan(&appRL, &tenantRL, &apiRL)
	if err != nil {
		return &models.QuotaRules{}, "default", nil
	}
	rules, source := rulesFromBlobs(appRL, tenantRL, apiRL)
	return rules, source, nil
}

func (r *PGRepository) HealthCheck(ctx context.Context) error {
	if r == nil || r.pool == nil {
		return fmt.Errorf("pg pool not initialized")
	}
	return r.pool.Ping(ctx)
}

// rulesFromBlobs is the pure parse+merge+source core of LoadRules, factored
// out so tests can drive it directly without a PG pool. Verbatim port of
// Python repository.load_rules' body (repository.py:99-114).
//
// Source priority: app_rl > tenant_rl > api_rl > "default", matching Python
// L105-112. A nil blob (NULL column) OR a truthy-false JSON value (`{}`,
// `[]`, `null`, `""`, `0`, `false`) means "layer not configured" and falls
// through to the next layer — this mirrors Python's `if row["app_rl"]:`
// truthiness check on the asyncpg-decoded jsonb value (empty dict is falsy).
// T1 review Minor #3: the prior `appRL != nil` reported source="app" for
// `{}` even though the layer contributed no rule — misleading for operators.
func rulesFromBlobs(appRL, tenantRL, apiRL []byte) (*models.QuotaRules, string) {
	apiRules := parseRulesBlob(apiRL)
	tenantRules := parseRulesBlob(tenantRL)
	appRules := parseRulesBlob(appRL)

	merged := mergeRules(mergeRules(apiRules, tenantRules), appRules)

	source := "default"
	switch {
	case isTruthyBlob(appRL):
		source = "app"
	case isTruthyBlob(tenantRL):
		source = "tenant"
	case isTruthyBlob(apiRL):
		source = "api_version"
	}
	return &merged, source
}

// parseRulesBlob ports Python _parse_rules_blob (repository.py:49-63). JSONB
// bytes → QuotaRules. NULL / empty / invalid JSON all yield QuotaRules{}
// (== EMPTY_RULES in Python). Each tier defaults to the zero-value LimitRule
// when absent in the blob.
func parseRulesBlob(raw []byte) models.QuotaRules {
	if len(raw) == 0 {
		return models.QuotaRules{}
	}
	var blob map[string]json.RawMessage
	if err := json.Unmarshal(raw, &blob); err != nil {
		return models.QuotaRules{}
	}
	return models.QuotaRules{
		Second: parseTier(blob["second"], tierSecondSecs, "second"),
		Minute: parseTier(blob["minute"], tierMinuteSecs, "minute"),
		Day:    parseTier(blob["day"], tierDaySecs, "day"),
	}
}

// isTruthyBlob mirrors Python's truthiness check on the jsonb column value:
// `if row["app_rl"]:` in repository.py:105. asyncpg's jsonb codec returns a
// Python object (dict / list / scalar), so empty-dict `{}` is falsy, NULL is
// None (falsy), but `{"x":1}` is truthy. We replicate that on raw pgx bytes:
//
//   - nil slice (NULL column)                  → false
//   - JSON `null`                              → false
//   - JSON `{}` (empty object)                 → false
//   - JSON `[]` (empty array)                  → false
//   - JSON `""` / `0` / `false`                → false
//   - any other valid JSON (incl. `{"second":...}`) → true
//   - invalid JSON                             → false (parseRulesBlob also
//                                                 returns EMPTY_RULES there)
//
// Without this, source reported "app" for an empty `{}` rate_limit even though
// the app layer contributed nothing — operators chasing "where did this rule
// come from?" would be misled (T1 review Minor #3).
func isTruthyBlob(raw []byte) bool {
	if len(raw) == 0 {
		return false
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	var v interface{}
	if err := dec.Decode(&v); err != nil {
		return false
	}
	switch x := v.(type) {
	case nil:
		return false
	case bool:
		return x
	case json.Number:
		return true // any number (incl. 0) — Python: `if 0:` is False, but a
		// jsonb rate_limit of `0` would also fail _parse_rules_blob and yield
		// EMPTY_RULES; the source label is then cosmetic. Keep truthy to
		// match Go's prior semantics for the (unspecified) bare-number case.
	case string:
		return x != ""
	case []interface{}:
		return len(x) > 0
	case map[string]interface{}:
		return len(x) > 0
	}
	return false
}

// parseTier ports Python _parse_tier (repository.py:23-46). Accepts:
//
//   - {"max_count": N, "window_seconds": W, "enabled": bool}  (canonical)
//   - {"max": N} or {"count": N}                              (shorthand)
//   - bare int / float                                        (max_count only)
//
// Returns a zero-value LimitRule when the tier is absent or has no usable
// max_count, so the limiter's "MaxCount<=0 → skip" rule treats it as inactive
// (matches Python returning None and the limiter skipping None tiers).
//
// Enabled defaults to true when the blob omits it (Python L45 default).
func parseTier(raw json.RawMessage, defaultWindow int64, tierName string) models.LimitRule {
	if len(raw) == 0 {
		return models.LimitRule{}
	}
	// Bare-number shorthand: Python L30-32.
	if n, ok := decodeNumber(raw); ok && n > 0 {
		return models.LimitRule{
			Tier:      tierName,
			MaxCount:  n,
			WindowSec: defaultWindow,
			WindowMs:  defaultWindow * 1000,
			Enabled:   true,
		}
	}
	// Dict form.
	var tier map[string]json.RawMessage
	if err := json.Unmarshal(raw, &tier); err != nil {
		return models.LimitRule{}
	}
	maxCount := firstInt(tier, "max_count", "max", "count")
	// Python L37-39: `max_count = raw.get("max_count") or raw.get("max") or raw.get("count")`
	// and L38-39 returns None when max_count is None — i.e. when none of the
	// keys is present (or all are 0, since `0 or x` falls through). We treat
	// maxCount<=0 as "tier not configured" for the same effect.
	if maxCount <= 0 {
		return models.LimitRule{}
	}
	window := defaultWindow
	if w := firstInt(tier, "window_seconds"); w > 0 {
		window = int64(w)
	}
	enabled := true
	if rawEnabled, ok := tier["enabled"]; ok {
		var b bool
		if err := json.Unmarshal(rawEnabled, &b); err == nil {
			enabled = b
		}
	}
	return models.LimitRule{
		Tier:      tierName,
		MaxCount:  maxCount,
		WindowSec: window,
		WindowMs:  window * 1000,
		Enabled:   enabled,
	}
}

// decodeNumber returns the integer value of a JSON number literal (int or
// float truncated to int). Returns ok=false for non-numeric JSON.
func decodeNumber(raw json.RawMessage) (int, bool) {
	var num json.Number
	if err := json.Unmarshal(raw, &num); err != nil {
		return 0, false
	}
	if i, err := num.Int64(); err == nil {
		return int(i), true
	}
	if f, err := num.Float64(); err == nil {
		return int(f), true
	}
	return 0, false
}

// firstInt returns the first non-zero int found under any of the given keys
// in the tier dict, mirroring Python's
// `raw.get("max_count") or raw.get("max") or raw.get("count")`. Returns 0 when
// none of the keys is present or all are 0.
func firstInt(m map[string]json.RawMessage, keys ...string) int {
	for _, k := range keys {
		raw, ok := m[k]
		if !ok || len(raw) == 0 {
			continue
		}
		if n, ok := decodeNumber(raw); ok {
			return n
		}
	}
	return 0
}

// mergeRules ports Python _merge (repository.py:66-72): override wins
// per-tier. A tier is considered "set" on override when its MaxCount > 0
// (Python encodes "not set" as None; Go encodes it as the zero-value
// LimitRule with MaxCount=0). The window + enabled of the winning side are
// carried along; base is only consulted when override is empty.
func mergeRules(base, override models.QuotaRules) models.QuotaRules {
	return models.QuotaRules{
		Second: mergeTier(base.Second, override.Second),
		Minute: mergeTier(base.Minute, override.Minute),
		Day:    mergeTier(base.Day, override.Day),
	}
}

func mergeTier(base, override models.LimitRule) models.LimitRule {
	if override.MaxCount > 0 {
		return override
	}
	return base
}
