package models

// === Request / Response — 与 Python 版一致 ===

type QuotaCheckRequest struct {
	TenantID string `json:"tenant_id"`
	AppID    string `json:"app_id"`
	APIID    string `json:"api_id"`
	Cost     int    `json:"cost"`
}

// QuotaCheckResponse aligns field-by-field with the Python contract
// (services/services/quota/src/quota/models.py::QuotaCheckResponse).
//
// JSON keys in declaration order: allowed, tier_blocked, limit, remaining,
// retry_after_seconds, rule_source. retry_after_seconds is non-zero only when
// Allowed=false (seconds remaining until the blocking tier's window resets).
//
// TierBlocked / Limit / Remaining are pointers so they serialize as JSON
// `null` when unset, mirroring Python's `str | None` / `int | None` (pydantic
// Optional). Python's allowed path leaves tier_blocked+limit None and sets
// remaining; Python's blocked path sets tier_blocked+limit and leaves
// remaining None. See limiter.CheckAndConsume for the branch-by-branch mapping.
type QuotaCheckResponse struct {
	Allowed           bool    `json:"allowed"`
	TierBlocked       *string `json:"tier_blocked"`
	Limit             *int    `json:"limit"`
	Remaining         *int    `json:"remaining"`
	RetryAfterSeconds int     `json:"retry_after_seconds"`
	RuleSource        string  `json:"rule_source"`
}

type QuotaRefundRequest struct {
	TenantID string `json:"tenant_id"`
	AppID    string `json:"app_id"`
	APIID    string `json:"api_id"`
	Cost     int    `json:"cost"`
}

type QuotaRefundResponse struct {
	Refunded bool `json:"refunded"`
}

// UsagePoint aligns with Python UsagePoint: {window_seconds, used, limit}.
// Limit is a pointer so it serializes as `null` when the tier is unconfigured
// (mirrors Python's `limit: int | None = None`).
type UsagePoint struct {
	WindowSeconds int64 `json:"window_seconds"`
	Used          int   `json:"used"`
	Limit         *int  `json:"limit"`
}

// UsageResponse is the flat shape Python returns: three named tier points
// (second / minute / day) rather than an array. Field order matches Python.
type UsageResponse struct {
	TenantID string     `json:"tenant_id"`
	AppID    string     `json:"app_id"`
	APIID    string     `json:"api_id"`
	Second   UsagePoint `json:"second"`
	Minute   UsagePoint `json:"minute"`
	Day      UsagePoint `json:"day"`
}

// === 限流规则 ===

// LimitRule mirrors Python quota.models.LimitRule. Enabled carries the
// per-tier enabled flag (Python L19: `enabled: bool = True`); the limiter
// treats MaxCount<=0 || !Enabled as "skip this tier" (matches Python
// _compile_rules L220: `if rule and rule.enabled and rule.max_count > 0`).
//
// The zero-value LimitRule (MaxCount=0, Enabled=false) represents an "unset"
// tier — equivalent to Python's None in QuotaRules. Parsed rules from the
// JSONB blob always carry Enabled=true unless the blob explicitly sets it
// false (see repository.parseTier).
type LimitRule struct {
	Tier      string
	MaxCount  int
	WindowSec int64
	WindowMs  int64
	Enabled   bool
}

// QuotaRules mirrors Python quota.models.QuotaRules. All-zero = unlimited
// (Python EMPTY_RULES, repository.py:20): the limiter's hasActive short-circuit
// fires when no tier has MaxCount>0 && Enabled, returning rule_source=
// "unlimited" without touching Redis.
type QuotaRules struct {
	Second LimitRule
	Minute LimitRule
	Day    LimitRule
}

// === Kafka ===

type QuotaEvent struct {
	TenantID    string `json:"tenant_id"`
	AppID       string `json:"app_id"`
	APIID       string `json:"api_id"`
	EventType   string `json:"event_type"`
	Allowed     bool   `json:"allowed"`
	TierBlocked string `json:"tier_blocked"`
	Cost        int    `json:"cost"`
}
