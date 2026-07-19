package main

import (
	"context"
	"crypto/subtle"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/redis/go-redis/v9"
	"github.com/segmentio/kafka-go"

	"github.com/abapplo/apihub/services/go/quota/internal/config"
	"github.com/abapplo/apihub/services/go/quota/internal/handler"
	"github.com/abapplo/apihub/services/go/quota/internal/limiter"
	"github.com/abapplo/apihub/services/go/quota/internal/repository"
)

func main() {
	cfg := config.Load()
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: parseLogLevel(cfg.LogLevel)})))

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	repo, err := repository.NewPGRepository(ctx, cfg.PGDSN(), cfg.PGPoolSize)
	if err != nil {
		slog.Error("pg_init_failed", "error", err)
		os.Exit(1)
	}
	defer repo.Close()

	rdb := redis.NewClient(&redis.Options{Addr: cfg.RedisAddr, Password: cfg.RedisPassword})
	if err := rdb.Ping(ctx).Err(); err != nil {
		slog.Error("redis_ping_failed", "error", err)
		os.Exit(1)
	}
	defer rdb.Close()

	var kw *kafka.Writer
	if cfg.KafkaBrokers != "" {
		kw = &kafka.Writer{Addr: kafka.TCP(cfg.KafkaBrokers), Balancer: &kafka.LeastBytes{}}
		defer kw.Close()
	}

	l := limiter.New(rdb, os.Getenv("HOME_REGION"), 0)
	h := handler.New(repo, l, kw)
	mux := http.NewServeMux()
	h.Register(mux)

	// Trust-ingress auth (R1d pattern, mirrors apihub_core.auth fast path):
	// APISIX key-auth validates the caller and rewrites X-Ingress-Auth=<secret>
	// onto the request; Go quota trusts that header and skips a per-request
	// auth round-trip. Security premise: Go quota is reachable only via
	// APISIX (ClusterIP, no external ingress) — same as dispatcher R1d.
	//
	// Fail-closed: if the operator forgets INGRESS_SHARED_SECRET, every non-
	// /health/live route 401s. We log a single startup warning so misconfig
	// is loud rather than silently open.
	if cfg.IngressSharedSecret == "" {
		slog.Warn("ingress_shared_secret_unset",
			"behavior", "deny_all_non_live_routes",
			"hint", "set INGRESS_SHARED_SECRET (same as dispatcher R1d) to open business routes")
	} else {
		slog.Info("ingress_auth_enabled")
	}

	server := &http.Server{
		Addr:         fmt.Sprintf(":%d", cfg.Port),
		Handler:      loggingMiddleware(recoveryMiddleware(ingressAuth(cfg.IngressSharedSecret, mux))),
		ReadTimeout:  5 * time.Second,
		WriteTimeout: 10 * time.Second,
		IdleTimeout:  60 * time.Second,
	}

	go func() {
		sig := make(chan os.Signal, 1)
		signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
		<-sig
		slog.Info("shutting down")
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		server.Shutdown(ctx)
	}()

	slog.Info("starting quota service", "port", cfg.Port)
	if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		slog.Error("server_error", "error", err)
		os.Exit(1)
	}
}

func loggingMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		next.ServeHTTP(w, r)
		slog.Info("request", "method", r.Method, "path", r.URL.Path, "duration", time.Since(start))
	})
}

func recoveryMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if err := recover(); err != nil {
				slog.Error("panic", "error", err)
				http.Error(w, `{"error":"internal"}`, 500)
			}
		}()
		next.ServeHTTP(w, r)
	})
}

// ingressAuth enforces the R1d trust-ingress contract: any request that did
// not transit the APISIX proxy (and thus lacks a matching X-Ingress-Auth
// header) is rejected with 401. The kubelet liveness AND readiness probes are
// the callers that legitimately bypass the gateway (probes carry no secret),
// so /health/live, /health/ready, AND /v1/quota/health are exempt — the last
// because deployment.yaml's readiness/liveness probes hit /v1/quota/health
// (kept as a ready-alias in handler/quota.go for callers that predate the
// standard /health/* names). Mirrors apihub_core's middleware
// skip_auth_paths=("/health",..., "/v1/quota/health") convention.
//
// Empty secret → fail closed. We do NOT log per-request here (the startup
// warning is enough); logging on every probe-able path would be noisy and
// itself an info leak.
func ingressAuth(secret string, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/health/live" || r.URL.Path == "/health/ready" || r.URL.Path == "/v1/quota/health" {
			next.ServeHTTP(w, r)
			return
		}
		if secret == "" || subtle.ConstantTimeCompare([]byte(r.Header.Get("X-Ingress-Auth")), []byte(secret)) != 1 {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusUnauthorized)
			_, _ = w.Write([]byte(`{"error":"unauthorized"}`))
			return
		}
		next.ServeHTTP(w, r)
	})
}

func parseLogLevel(level string) slog.Level {
	switch level {
	case "debug":
		return slog.LevelDebug
	case "warn":
		return slog.LevelWarn
	case "error":
		return slog.LevelError
	default:
		return slog.LevelInfo
	}
}
