.PHONY: help install dev fmt lint test docker-build tf-init tf-plan tf-apply \
        k8s-apply-dev k8s-apply-staging k8s-apply-prod argocd-sync \
        run-registry run-dispatcher run-auth run-executor run-quota run-tenant run-admin \
        dev-up dev-down dev-logs dev-ps dev-reset dev-psql dev-redis-cli

SHELL := /bin/bash

ENV ?= dev
SERVICE ?= api-registry
# 优先 docker compose v2，回退 v1
COMPOSE := $(shell \
	command -v "docker" >/dev/null 2>&1 && docker compose version >/dev/null 2>&1 \
		&& echo "docker compose --env-file .env.dev -f docker-compose.dev.yml" \
		|| echo "docker-compose --env-file .env.dev -f docker-compose.dev.yml")

help:  ## 显示所有可用目标
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

install:  ## 安装所有服务依赖（开发模式）
	cd services/libs/apihub-core && pip install -e .
	cd services/services/api-registry && pip install -e .
	@echo "✅ 安装完成"

dev: install  ## 一键启动开发环境（依赖 docker compose 起 PG/Redis/Kafka）

fmt:  ## 格式化代码
	ruff format services/
	ruff check --fix services/

lint:  ## 代码检查
	ruff check services/
	mypy services/

test:  ## 跑测试
	pytest services/ -v --cov=apihub_core --cov-report=term-missing

# ===== Docker =====
docker-build:  ## 构建服务镜像（在仓库根目录执行）
	docker build -f services/services/$(SERVICE)/Dockerfile \
		-t registry.apihub.internal/apihub/$(SERVICE):0.1.0-dev .

# ===== Terraform =====
TF_DIR = deploy/terraform/envs/$(ENV)

tf-init:  ## terraform init
	cd $(TF_DIR) && terraform init

tf-plan:  ## terraform plan
	cd $(TF_DIR) && terraform plan

tf-apply:  ## terraform apply（生产环境请走 PR）
	cd $(TF_DIR) && terraform apply

tf-destroy:  ## terraform destroy（仅 dev）
	@[ "$(ENV)" = "prod" ] && echo "❌ 禁止在 prod 执行 destroy" && exit 1 || \
		cd $(TF_DIR) && terraform destroy

# ===== K8s =====
k8s-apply-dev:  ## 同步 dev 环境（本地 kubectl）
	kustomize build deploy/k8s/overlays/dev | kubectl apply -f -

k8s-apply-staging:
	kustomize build deploy/k8s/overlays/staging | kubectl apply -f -

k8s-apply-prod:
	kustomize build deploy/k8s/overlays/prod | kubectl apply -f -

argocd-sync:  ## 让 ArgoCD 同步（需先有 argocd CLI）
	argocd app sync apihub-$(ENV)

# ===== 本地运行服务 =====
run-registry:  ## 本地启动 api-registry
	uvicorn api_registry.main:app --reload --port 8000

run-dispatcher:  ## 本地启动 dispatcher
	uvicorn dispatcher.main:app --reload --port 8001

run-auth:  ## 本地启动 auth
	uvicorn auth.main:app --reload --port 8002

run-executor:  ## 本地启动 executor（worker，需要 Kafka + PG + Redis）
	uvicorn executor.main:app --reload --port 8003

run-quota:  ## 本地启动 quota（延迟敏感，需要 Redis）
	uvicorn quota.main:app --reload --port 8004

run-tenant:  ## 本地启动 tenant-svc（管理类，需要 PG + Redis）
	uvicorn tenant.main:app --reload --port 8005

run-admin:  ## 本地启动 admin-bff（聚合 + 审计，需要 PG + tenant-svc）
	uvicorn admin.main:app --reload --port 8006

# ===== Dev Stack (docker compose) =====
dev-up:  ## 启动开发栈（PG/Redis/Kafka/CH/MinIO/Jaeger/Grafana）
	@test -f .env.dev || cp .env.dev.example .env.dev
	$(COMPOSE) up -d
	@echo ""
	@echo "✅ 开发栈已启动："
	@echo "   PG          postgresql://apihub:apihub_dev_pwd@localhost:5432/apihub"
	@echo "   Redis       redis-cli -a apihub_dev_pwd -h localhost -p 6379"
	@echo "   Kafka       localhost:9094  (UI: http://localhost:9001  MinIO)"
	@echo "   ClickHouse  http://localhost:8123  (user=apihub)"
	@echo "   MinIO       http://localhost:9001  (apihub/apihub_dev_pwd)"
	@echo "   Jaeger      http://localhost:16686"
	@echo "   Grafana     http://localhost:3000  (admin/$(shell grep GRAFANA_PASSWORD .env.dev | cut -d= -f2))"
	@echo ""
	@echo "   停止： make dev-down"

dev-down:  ## 停止开发栈（保留 volume）
	$(COMPOSE) down

dev-logs:  ## 看开发栈日志（tail -f）
	$(COMPOSE) logs -f --tail=100

dev-ps:  ## 看开发栈状态
	$(COMPOSE) ps

dev-reset:  ## ⚠️ 清掉所有 volume 重建（dev only）
	@read -p "This wipes all dev data. Continue? [y/N] " ans; \
		[ "$$ans" = "y" ] || { echo "Aborted"; exit 1; }
	$(COMPOSE) down -v
	$(COMPOSE) up -d

dev-psql:  ## 进 PG psql
	PGPASSWORD=$$(grep PG_PASSWORD .env.dev | cut -d= -f2) \
		psql -h localhost -U $$(grep PG_USER .env.dev | cut -d= -f2) \
		     -d $$(grep PG_DATABASE .env.dev | cut -d= -f2)

dev-redis-cli:  ## 进 Redis CLI
	redis-cli -h localhost -p 6379 -a $$(grep REDIS_PASSWORD .env.dev | cut -d= -f2)
