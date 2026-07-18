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
type QuotaCheckResponse struct {
	Allowed           bool   `json:"allowed"`
	TierBlocked       string `json:"tier_blocked"`
	Limit             int    `json:"limit"`
	Remaining         int    `json:"remaining"`
	RetryAfterSeconds int    `json:"retry_after_seconds"`
	RuleSource        string `json:"rule_source"`
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

type LimitRule struct {
	Tier      string
	MaxCount  int
	WindowSec int64
	WindowMs  int64
}

type QuotaRules struct {
	Second LimitRule
	Minute LimitRule
	Day    LimitRule
}

type RuleRow struct {
	SecondMax      int   `db:"second_max"`
	SecondWindowMs int64 `db:"second_window_ms"`
	MinuteMax      int   `db:"minute_max"`
	MinuteWindowMs int64 `db:"minute_window_ms"`
	DayMax         int   `db:"day_max"`
	DayWindowMs    int64 `db:"day_window_ms"`
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
