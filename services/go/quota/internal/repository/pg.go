package repository

import (
	"context"
	"fmt"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/abapplo/apihub/services/go/quota/internal/models"
)

type PGRepository struct {
	pool *pgxpool.Pool
}

func NewPGRepository(ctx context.Context, dsn string, poolSize int) (*PGRepository, error) {
	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		return nil, fmt.Errorf("parse dsn: %w", err)
	}
	cfg.MaxConns = int32(poolSize)
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

func (r *PGRepository) LoadRules(ctx context.Context, tenantID, appID, apiID string) (*models.QuotaRules, string, error) {
	var row models.RuleRow
	err := r.pool.QueryRow(ctx,
		`SELECT second_max, second_window_ms, minute_max, minute_window_ms,
		        day_max, day_window_ms
		 FROM quota_rule
		 WHERE tenant_id = $1 AND app_id = $2 AND api_id = $3
		   AND status = 'active'`,
		tenantID, appID, apiID,
	).Scan(&row.SecondMax, &row.SecondWindowMs, &row.MinuteMax, &row.MinuteWindowMs, &row.DayMax, &row.DayWindowMs)

	if err != nil {
		// No quota_rule row for this (tenant, app, api) → built-in defaults.
		// Source label mirrors Python repository.load_rules' "default" branch
		// (services/services/quota/src/quota/repository.py:111-112): when no
		// layer contributed a rate_limit, source="default". The handler rewrites
		// rule_source from this unless the limiter already said "fallback"
		// (Redis failure) — see handler.QuotaHandler.check. The previous label
		// "fallback" collided with the limiter's Redis-failure semantics and
		// broke the Python rule_source contract (concern-2 surfaced by R3a T5).
		return defaultRules(), "default", nil
	}

	// Source label "api_version" mirrors Python repository.load_rules'
	// api-version-rule branch (services/services/quota/src/quota/repository.py:110);
	// "api" was not in Python's rule_source contract set
	// {app, tenant, api_version, default, fallback} (concern-a, R3a T5).
	//
	// Asymmetry note (concern-a, deferred to R3b): Python's load_rules merges
	// three disjunctive layers — app_rl / tenant_rl / api_rl — and labels the
	// winner ("app" > "tenant" > "api_version"). Go's quota_rule table is a
	// single row keyed by (tenant_id, app_id, api_id) with no per-layer merge,
	// so today only the "api_version" label is reachable on this branch. The
	// app/tenant layers and their labels are not yet implemented in Go.
	return &models.QuotaRules{
		Second: models.LimitRule{Tier: "second", MaxCount: row.SecondMax, WindowMs: row.SecondWindowMs, WindowSec: row.SecondWindowMs / 1000},
		Minute: models.LimitRule{Tier: "minute", MaxCount: row.MinuteMax, WindowMs: row.MinuteWindowMs, WindowSec: row.MinuteWindowMs / 1000},
		Day:    models.LimitRule{Tier: "day", MaxCount: row.DayMax, WindowMs: row.DayWindowMs, WindowSec: row.DayWindowMs / 1000},
	}, "api_version", nil
}

func defaultRules() *models.QuotaRules {
	return &models.QuotaRules{
		Second: models.LimitRule{Tier: "second", MaxCount: 10, WindowMs: 1000, WindowSec: 1},
		Minute: models.LimitRule{Tier: "minute", MaxCount: 100, WindowMs: 60000, WindowSec: 60},
		Day:    models.LimitRule{Tier: "day", MaxCount: 1000, WindowMs: 86400000, WindowSec: 86400},
	}
}

func (r *PGRepository) HealthCheck(ctx context.Context) error {
	if r == nil || r.pool == nil {
		return fmt.Errorf("pg pool not initialized")
	}
	return r.pool.Ping(ctx)
}
