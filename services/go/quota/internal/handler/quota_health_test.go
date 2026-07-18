package handler

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/alicebob/miniredis/v2"
	"github.com/redis/go-redis/v9"

	"github.com/abapplo/apihub/services/go/quota/internal/limiter"
	"github.com/abapplo/apihub/services/go/quota/internal/repository"
)

// TestLiveHandler_Always200 verifies the liveness probe shape: 200 + JSON
// envelope, regardless of dependency state. live MUST NOT touch PG/Redis —
// a broken dep must not turn a liveness fail into a pod-restart loop.
func TestLiveHandler_Always200(t *testing.T) {
	h := &QuotaHandler{} // zero-value: nil repo/limiter/kafka — live must not care

	req := httptest.NewRequest(http.MethodGet, "/health/live", nil)
	w := httptest.NewRecorder()
	h.live(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("status: want 200, got %d", w.Code)
	}
	if ct := w.Header().Get("Content-Type"); ct != "application/json" {
		t.Fatalf("content-type: want application/json, got %q", ct)
	}
	body := w.Body.String()
	if !strings.Contains(body, `"status":"ok"`) || !strings.Contains(body, `"service":"quota"`) {
		t.Fatalf("body: want status=ok+service=quota, got %q", body)
	}
}

// TestReadyHandler_503WhenDepsMissing: both PG and Redis nil → 503.
// Confirms the readiness gate fails closed when neither dep is wired.
func TestReadyHandler_503WhenDepsMissing(t *testing.T) {
	h := &QuotaHandler{
		repo:    &repository.PGRepository{}, // nil pool → HealthCheck returns err
		limiter: &limiter.Limiter{},         // nil redis → HealthCheck returns err
	}

	req := httptest.NewRequest(http.MethodGet, "/health/ready", nil)
	w := httptest.NewRecorder()
	h.ready(w, req)

	if w.Code != http.StatusServiceUnavailable {
		t.Fatalf("status: want 503, got %d", w.Code)
	}
}

// TestReadyHandler_503WhenOnlyPGDown proves /health/ready AND-semantics: even
// with Redis healthy (miniredis-backed Limiter), a failing PG ping must yield
// 503. The 200 branch needs a live PG pool — covered by integration tests
// (tests/quota_test.go style), not reachable from a pure unit setup.
func TestReadyHandler_503WhenOnlyPGDown(t *testing.T) {
	mr, err := miniredis.Run()
	if err != nil {
		t.Fatalf("miniredis: %v", err)
	}
	defer mr.Close()

	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	defer rdb.Close()

	h := &QuotaHandler{
		repo:    &repository.PGRepository{}, // PG still down
		limiter: limiter.New(rdb, "", 0),    // Redis healthy
	}

	req := httptest.NewRequest(http.MethodGet, "/health/ready", nil)
	w := httptest.NewRecorder()
	h.ready(w, req)

	if w.Code != http.StatusServiceUnavailable {
		t.Fatalf("status: want 503 (PG down → not ready), got %d", w.Code)
	}
}

// TestRegister_IncludesHealthRoutes confirms the new /health/* routes plus the
// legacy /v1/quota/health alias get wired onto the mux. Business routes are
// exercised by their own tests; here we only verify the health surface (the
// thing this task added) — and the handlers are safe to invoke with a
// zero-value QuotaHandler (live ignores deps; ready is nil-safe per
// Limiter/PGRepository HealthCheck).
func TestRegister_IncludesHealthRoutes(t *testing.T) {
	h := &QuotaHandler{}
	mux := http.NewServeMux()
	h.Register(mux)

	cases := []struct {
		method, path string
		wantStatus  int
	}{
		{http.MethodGet, "/health/live", http.StatusOK},
		{http.MethodGet, "/health/ready", http.StatusServiceUnavailable}, // deps nil → 503
		{http.MethodGet, "/v1/quota/health", http.StatusServiceUnavailable},
	}
	for _, c := range cases {
		req := httptest.NewRequest(c.method, c.path, nil)
		w := httptest.NewRecorder()
		mux.ServeHTTP(w, req)
		if w.Code == http.StatusNotFound {
			t.Errorf("%s %s: route not registered (404)", c.method, c.path)
		}
		if w.Code != c.wantStatus {
			t.Errorf("%s %s: status want %d, got %d", c.method, c.path, c.wantStatus, w.Code)
		}
	}
}

