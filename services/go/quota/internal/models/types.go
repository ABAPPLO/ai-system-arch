package models

// === Request / Response — 与 Python 版一致 ===

type QuotaCheckRequest struct {
	TenantID string `json:"tenant_id"`
	AppID    string `json:"app_id"`
	APIID    string `json:"api_id"`
	Cost     int    `json:"cost"`
}

type QuotaCheckResponse struct {
	Allowed     bool   `json:"allowed"`
	TierBlocked string `json:"tier_blocked"`
	Current     int    `json:"current"`
	Limit       int    `json:"limit"`
	Remaining   int    `json:"remaining"`
	ResetMs     int64  `json:"reset_ms"`
	RuleSource  string `json:"rule_source"`
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

type UsagePoint struct {
	Tier      string `json:"tier"`
	Used      int    `json:"used"`
	Limit     int    `json:"limit"`
	Remaining int    `json:"remaining"`
	ResetMs   int64  `json:"reset_ms"`
}

type UsageResponse struct {
	Points []UsagePoint `json:"points"`
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
