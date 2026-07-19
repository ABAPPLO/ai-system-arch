package config

import (
	"fmt"
	"os"
	"strconv"
)

type Config struct {
	Port          int
	PGHost        string
	PGPort        int
	PGUser        string
	PGPassword    string
	PGDatabase    string
	PGPoolSize    int
	RedisAddr     string
	RedisPassword string
	KafkaBrokers  string
	LogLevel      string
	// IngressSharedSecret is the shared secret injected by APISIX
	// proxy-rewrite as the `X-Ingress-Auth` request header. APISIX key-auth
	// validates the caller's API key first; Go quota then trusts the header
	// and skips a per-request auth round-trip. Mirrors R1d
	// (apihub_core.config.ingress_shared_secret). Empty → fail closed
	// (deny all non-/health/live routes); see cmd/main.go ingressAuth.
	IngressSharedSecret string
}

func Load() *Config {
	return &Config{
		Port:               envInt("PORT", 8004),
		PGHost:             envStr("PG_HOST", "localhost"),
		PGPort:             envInt("PG_PORT", 5432),
		PGUser:             envStr("PG_USER", "apihub"),
		PGPassword:         envStr("PG_PASSWORD", ""),
		PGDatabase:         envStr("PG_DATABASE", "apihub"),
		PGPoolSize:         envInt("PG_POOL_MAX", envInt("PG_POOL_SIZE", 10)),
		RedisAddr:          resolveRedisAddr(),
		RedisPassword:      envStr("REDIS_PASSWORD", ""),
		KafkaBrokers:       envStr("KAFKA_BROKERS", ""),
		LogLevel:           envStr("LOG_LEVEL", "info"),
		IngressSharedSecret: os.Getenv("INGRESS_SHARED_SECRET"),
	}
}

// resolveRedisAddr composes the Redis address from the cluster convention
// (REDIS_HOST + REDIS_PORT, set by apihub-shared-infra / quota-config CMs)
// and falls back to a direct REDIS_ADDR env, then to localhost:6379.
// Mirrors apihub_core.config.Settings.redis_host + redis_port → addr.
func resolveRedisAddr() string {
	if v := os.Getenv("REDIS_ADDR"); v != "" {
		return v
	}
	host := envStr("REDIS_HOST", "localhost")
	port := envInt("REDIS_PORT", 6379)
	return fmt.Sprintf("%s:%d", host, port)
}

func (c *Config) PGDSN() string {
	return fmt.Sprintf("postgres://%s:%s@%s:%d/%s?sslmode=disable",
		c.PGUser, c.PGPassword, c.PGHost, c.PGPort, c.PGDatabase)
}

func envStr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func envInt(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}
