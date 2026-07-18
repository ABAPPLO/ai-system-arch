package handler

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"time"

	"github.com/segmentio/kafka-go"

	"github.com/abapplo/apihub/services/go/quota/internal/limiter"
	"github.com/abapplo/apihub/services/go/quota/internal/models"
	"github.com/abapplo/apihub/services/go/quota/internal/repository"
)

type QuotaHandler struct {
	repo    *repository.PGRepository
	limiter *limiter.Limiter
	kafka   *kafka.Writer
}

func New(repo *repository.PGRepository, l *limiter.Limiter, kw *kafka.Writer) *QuotaHandler {
	return &QuotaHandler{repo: repo, limiter: l, kafka: kw}
}

func (h *QuotaHandler) Register(mux *http.ServeMux) {
	mux.HandleFunc("POST /v1/quota/check", h.check)
	mux.HandleFunc("POST /v1/quota/check-strict", h.checkStrict)
	mux.HandleFunc("POST /v1/quota/refund", h.refund)
	mux.HandleFunc("GET /v1/quota/usage", h.usage)
	// Health endpoints. /health/live is liveness (always 200, bypasses auth
	// — see cmd/main.go ingressAuth). /health/ready is readiness (PG + Redis
	// ping, 200/503). /v1/quota/health kept as a ready-alias for callers that
	// predate the standard /health/* names.
	mux.HandleFunc("GET /health/live", h.live)
	mux.HandleFunc("GET /health/ready", h.ready)
	mux.HandleFunc("GET /v1/quota/health", h.ready)
}

func writeJSON(w http.ResponseWriter, status int, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(v)
}

func (h *QuotaHandler) check(w http.ResponseWriter, r *http.Request) {
	var req models.QuotaCheckRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, 400, map[string]string{"error": "invalid json"})
		return
	}
	// source (api/fallback provenance from LoadRules) is intentionally
	// ignored here: Limiter.CheckAndConsume already emits "rules"/"unlimited"
	// /"fallback", and T1 deliberately diverged from Python routes.py:58-60
	// (which rewrites rule_source to repo `source`). Reconciling belongs to a
	// dedicated task — kept off R3a to avoid changing observable behavior.
	rules, _, err := h.repo.LoadRules(r.Context(), req.TenantID, req.AppID, req.APIID)
	if err != nil {
		slog.Error("load_rules_error", "error", err)
		writeJSON(w, 500, map[string]string{"error": "internal"})
		return
	}
	resp := h.limiter.CheckAndConsume(r.Context(), req.TenantID, req.AppID, req.APIID, rules, req.Cost)
	if h.kafka != nil {
		go h.emitQuotaEvent(req, resp.Allowed, tierBlockedOrEmpty(resp.TierBlocked))
	}
	writeJSON(w, 200, resp)
}

func (h *QuotaHandler) checkStrict(w http.ResponseWriter, r *http.Request) {
	var req models.QuotaCheckRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, 400, map[string]string{"error": "invalid json"})
		return
	}
	rules, source, err := h.repo.LoadRules(r.Context(), req.TenantID, req.AppID, req.APIID)
	if err != nil {
		slog.Error("load_rules_error", "error", err)
		writeJSON(w, 500, map[string]string{"error": "internal"})
		return
	}
	resp := h.limiter.CheckAndConsume(r.Context(), req.TenantID, req.AppID, req.APIID, rules, req.Cost)
	resp.RuleSource = source
	if !resp.Allowed {
		writeJSON(w, 429, resp)
		return
	}
	if h.kafka != nil {
		go h.emitQuotaEvent(req, resp.Allowed, tierBlockedOrEmpty(resp.TierBlocked))
	}
	writeJSON(w, 200, resp)
}

// tierBlockedOrEmpty dereferences a nilable TierBlocked for the Kafka event
// payload, which carries the tier name as a plain string (nil → "").
func tierBlockedOrEmpty(p *string) string {
	if p == nil {
		return ""
	}
	return *p
}

func (h *QuotaHandler) refund(w http.ResponseWriter, r *http.Request) {
	var req models.QuotaRefundRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, 400, map[string]string{"error": "invalid json"})
		return
	}
	ok := h.limiter.Refund(r.Context(), req.TenantID, req.AppID, req.APIID, req.Cost)
	writeJSON(w, 200, models.QuotaRefundResponse{Refunded: ok})
}

func (h *QuotaHandler) usage(w http.ResponseWriter, r *http.Request) {
	tenantID := r.URL.Query().Get("tenant_id")
	appID := r.URL.Query().Get("app_id")
	apiID := r.URL.Query().Get("api_id")
	rules, _, err := h.repo.LoadRules(r.Context(), tenantID, appID, apiID)
	if err != nil {
		slog.Error("load_rules_error", "error", err)
		writeJSON(w, 500, map[string]string{"error": "internal"})
		return
	}
	resp := h.limiter.GetUsage(r.Context(), tenantID, appID, apiID, rules)
	writeJSON(w, 200, resp)
}

// live is the liveness probe: always 200 if the process is serving. No
// dependency checks — kubelet uses this to decide restart.
func (h *QuotaHandler) live(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, 200, map[string]string{"status": "ok", "service": "quota"})
}

// ready is the readiness probe: 200 only when both PG and Redis answer.
// 503 otherwise so k8s pulls the pod out of the Service endpoints until
// deps recover. Also backs the legacy /v1/quota/health route.
func (h *QuotaHandler) ready(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	pgErr := h.repo.HealthCheck(ctx)
	redisErr := h.limiter.HealthCheck(ctx)
	if pgErr != nil || redisErr != nil {
		slog.Error("ready_check_failed", "pg_error", pgErr, "redis_error", redisErr)
		writeJSON(w, 503, map[string]string{"status": "unhealthy"})
		return
	}
	writeJSON(w, 200, map[string]string{"status": "ok", "service": "quota"})
}

func (h *QuotaHandler) emitQuotaEvent(req models.QuotaCheckRequest, allowed bool, tierBlocked string) {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	event := models.QuotaEvent{
		TenantID: req.TenantID, AppID: req.AppID, APIID: req.APIID,
		EventType: "quota_check", Allowed: allowed, TierBlocked: tierBlocked, Cost: req.Cost,
	}
	data, _ := json.Marshal(event)
	err := h.kafka.WriteMessages(ctx, kafka.Message{
		Topic: "api-call-events",
		Key:   []byte(req.TenantID + ":" + req.AppID + ":" + req.APIID),
		Value: data,
	})
	if err != nil {
		slog.Warn("kafka_emit_failed", "error", err)
	}
}
