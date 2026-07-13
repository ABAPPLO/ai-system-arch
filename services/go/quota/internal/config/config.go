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
}

func Load() *Config {
	return &Config{
		Port:          envInt("PORT", 8004),
		PGHost:        envStr("PG_HOST", "localhost"),
		PGPort:        envInt("PG_PORT", 5432),
		PGUser:        envStr("PG_USER", "apihub"),
		PGPassword:    envStr("PG_PASSWORD", ""),
		PGDatabase:    envStr("PG_DATABASE", "apihub"),
		PGPoolSize:    envInt("PG_POOL_SIZE", 10),
		RedisAddr:     envStr("REDIS_ADDR", "localhost:6379"),
		RedisPassword: envStr("REDIS_PASSWORD", ""),
		KafkaBrokers:  envStr("KAFKA_BROKERS", ""),
		LogLevel:      envStr("LOG_LEVEL", "info"),
	}
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
