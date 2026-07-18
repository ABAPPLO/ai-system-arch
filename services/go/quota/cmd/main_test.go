package main

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// okHandler is a minimal 200 responder used as the inner handler for
// middleware tests — we only care about auth gating, not business routing.
var okHandler = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte(`{"ok":true}`))
})

// --- ingressAuth: header check ----------------------------------------------

func TestIngressAuth_NoHeader_Returns401(t *testing.T) {
	h := ingressAuth("s3cr3t", okHandler)
	req := httptest.NewRequest(http.MethodPost, "/v1/quota/check", nil)
	w := httptest.NewRecorder()
	h.ServeHTTP(w, req)

	if w.Code != http.StatusUnauthorized {
		t.Fatalf("status: want 401, got %d", w.Code)
	}
	if body := w.Body.String(); !strings.Contains(body, `"error":"unauthorized"`) {
		t.Fatalf("body: want {\"error\":\"unauthorized\"}, got %q", body)
	}
	if ct := w.Header().Get("Content-Type"); ct != "application/json" {
		t.Fatalf("content-type: want application/json, got %q", ct)
	}
}

func TestIngressAuth_WrongSecret_Returns401(t *testing.T) {
	h := ingressAuth("s3cr3t", okHandler)
	req := httptest.NewRequest(http.MethodPost, "/v1/quota/check", nil)
	req.Header.Set("X-Ingress-Auth", "wrong")
	w := httptest.NewRecorder()
	h.ServeHTTP(w, req)

	if w.Code != http.StatusUnauthorized {
		t.Fatalf("status: want 401, got %d", w.Code)
	}
}

func TestIngressAuth_CorrectSecret_Returns200(t *testing.T) {
	h := ingressAuth("s3cr3t", okHandler)
	req := httptest.NewRequest(http.MethodPost, "/v1/quota/check", nil)
	req.Header.Set("X-Ingress-Auth", "s3cr3t")
	w := httptest.NewRecorder()
	h.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("status: want 200, got %d", w.Code)
	}
}

func TestIngressAuth_EmptySecret_FailClosed(t *testing.T) {
	// Misconfig / dev-without-env: secret empty → deny all business routes.
	// Critical: must NOT silently allow (would expose unauthenticated quota).
	h := ingressAuth("", okHandler)

	cases := []struct {
		name       string
		header     string
		wantStatus int
	}{
		{"no header", "", http.StatusUnauthorized},
		{"header present but secret unset", "anything", http.StatusUnauthorized},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			req := httptest.NewRequest(http.MethodPost, "/v1/quota/check", nil)
			if c.header != "" {
				req.Header.Set("X-Ingress-Auth", c.header)
			}
			w := httptest.NewRecorder()
			h.ServeHTTP(w, req)
			if w.Code != c.wantStatus {
				t.Fatalf("status: want %d, got %d", c.wantStatus, w.Code)
			}
		})
	}
}

// --- ingressAuth: health bypass ---------------------------------------------

func TestIngressAuth_HealthLive_BypassesAuth(t *testing.T) {
	// Kubelet liveness probe carries no secret — must pass even when auth is on.
	h := ingressAuth("s3cr3t", okHandler)
	req := httptest.NewRequest(http.MethodGet, "/health/live", nil)
	w := httptest.NewRecorder()
	h.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("status: want 200 (live bypass), got %d", w.Code)
	}
}

func TestIngressAuth_HealthReady_BypassesAuth(t *testing.T) {
	// Readiness probe also carries no secret — mirrors apihub_core
	// skip_auth_paths=("/health",...) startswith convention.
	h := ingressAuth("s3cr3t", okHandler)
	req := httptest.NewRequest(http.MethodGet, "/health/ready", nil)
	w := httptest.NewRecorder()
	h.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("status: want 200 (ready bypass), got %d", w.Code)
	}
}

func TestIngressAuth_QuotaHealth_BypassesAuth(t *testing.T) {
	// deployment.yaml readiness/liveness probes hit /v1/quota/health, and the
	// kubelet carries no X-Ingress-Auth — so this route MUST bypass auth, same
	// as /health/ready (it is registered as a ready-alias in handler/quota.go).
	// Mirrors Python's skip_auth_paths which lists /v1/quota/health.
	h := ingressAuth("s3cr3t", okHandler)
	req := httptest.NewRequest(http.MethodGet, "/v1/quota/health", nil)
	w := httptest.NewRecorder()
	h.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("status: want 200 (probe-able ready-alias bypass), got %d", w.Code)
	}
}

// --- middleware chain ordering ----------------------------------------------

// TestChain_PanicDoesNotBypassAuth ensures recovery+ingressAuth compose safely:
// an inner panic must NOT leak past recovery, and recovery must NOT be
// reachable without the ingress header (auth is the OUTER gate).
func TestChain_PanicRecoveredAfterAuth(t *testing.T) {
	panicHandler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		panic("boom")
	})
	chain := loggingMiddleware(recoveryMiddleware(ingressAuth("s3cr3t", panicHandler)))

	// Without header: 401 before the inner handler runs → no panic.
	req := httptest.NewRequest(http.MethodPost, "/v1/quota/check", nil)
	w := httptest.NewRecorder()
	chain.ServeHTTP(w, req)
	if w.Code != http.StatusUnauthorized {
		t.Fatalf("no-header: want 401, got %d", w.Code)
	}

	// With header: inner panics → recovery returns 500.
	req2 := httptest.NewRequest(http.MethodPost, "/v1/quota/check", nil)
	req2.Header.Set("X-Ingress-Auth", "s3cr3t")
	w2 := httptest.NewRecorder()
	chain.ServeHTTP(w2, req2)
	if w2.Code != http.StatusInternalServerError {
		t.Fatalf("panic: want 500, got %d", w2.Code)
	}
	body, _ := io.ReadAll(w2.Body)
	if !strings.Contains(string(body), "internal") {
		t.Fatalf("panic body: want {\"error\":\"internal\"}, got %q", string(body))
	}
}
