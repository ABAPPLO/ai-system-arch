.PHONY: help install dev fmt lint test docker-build tf-init tf-plan tf-apply \
        k8s-apply-dev k8s-apply-staging k8s-apply-prod argocd-sync \
        run-registry run-dispatcher run-auth run-executor run-quota build-quota run-tenant run-admin run-docs run-trace run-retry run-workflow run-notification run-portal run-ai-gateway run-billing \
        run-admin-frontend admin-frontend-install admin-frontend-typecheck admin-frontend-build portal-frontend-install portal-frontend-typecheck portal-frontend-build run-portal-frontend \
        dev-up dev-down dev-logs dev-ps dev-reset dev-psql dev-redis-cli db-apply \
        cli-install cli-validate cli-apply-dev cli-apply-staging cli-apply-prod \
        alerts-validate \
        rollout-status rollout-promote rollout-pause rollout-abort rollout-undo

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
	pip install -e tools/apihub-cli/
	@echo "✅ 安装完成"

# ===== apihub-cli =====
cli-install:  ## 安装声明式接入 CLI
	pip install -e tools/apihub-cli/

cli-validate:  ## 校验 schema/ 下所有 YAML（不调远端）
	apihub-apply validate schema/

cli-apply-dev:  ## 推 schema/ 到 dev 环境
	apihub-apply schema/ --env dev --submitted-by "$(USER)@local"

cli-apply-staging:  ## 推 schema/ 到 staging（待审批）
	apihub-apply schema/ --env staging --submitted-by "$(USER)@local"

cli-apply-prod:  ## 推 schema/ 到 prod（强审批）
	apihub-apply schema/ --env prod --submitted-by "$(USER)@local"

# ===== Alerts =====
alerts-validate:  ## 校验 Prometheus 告警规则文件（CI 用）
	python scripts/validate-alerts.py

# ===== Argo Rollouts（canary 灰度）=====
ROLLOUT ?= dispatcher
ROLLOUT_NS ?= apihub-system

rollout-status:  ## 看灰度状态（终端 UI）
	kubectl argo rollouts get rollout $(ROLLOUT) -n $(ROLLOUT_NS)

rollout-promote:  ## 推进到下一步灰度
	kubectl argo rollouts promote $(ROLLOUT) -n $(ROLLOUT_NS)

rollout-pause:  ## 暂停灰度（保留现场观察）
	kubectl argo rollouts pause $(ROLLOUT) -n $(ROLLOUT_NS)

rollout-abort:  ## 中断灰度并立即回滚到 stable
	kubectl argo rollouts abort $(ROLLOUT) -n $(ROLLOUT_NS)

rollout-undo:  ## 回滚到上一个 revision
	kubectl argo rollouts undo $(ROLLOUT) -n $(ROLLOUT_NS)

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
	@case "$(SERVICE)" in \
	  quota) \
	    echo ">> quota uses Go impl: services/go/quota/Dockerfile (context=services/go/quota)"; \
	    docker build -f services/go/quota/Dockerfile \
	      -t registry.apihub.internal/apihub/quota:0.1.0-dev services/go/quota ;; \
	  *) \
	    docker build -f services/services/$(SERVICE)/Dockerfile \
	      -t registry.apihub.internal/apihub/$(SERVICE):0.1.0-dev . ;; \
	esac

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
# 所有 apply 经 scripts/k8s/apply.sh（唯一入口；直接 kubectl apply -f base 会 revert overlay）。
k8s-apply-kind:  ## 同步本地 kind 集群（注入 host IP + read-back 端口）
	bash scripts/k8s/apply.sh kind

k8s-apply-dev:  ## 同步 dev 环境（ACK）
	bash scripts/k8s/apply.sh dev

k8s-apply-staging:  ## 同步 staging 环境
	bash scripts/k8s/apply.sh staging

k8s-apply-prod:  ## 同步 prod 环境
	bash scripts/k8s/apply.sh prod

k8s-check-kind:  ## 自检 kind overlay 关键字段未被 revert
	bash scripts/k8s/check-overlay.sh kind

argocd-setup:  ## 在 kind 装 ArgoCD（GitOps 控制面）
	bash scripts/k8s/argocd-setup.sh

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

run-quota:  ## 本地启动 quota（Go 实现，延迟敏感，需要 Redis）
	cd services/go/quota && go run ./cmd

build-quota:  ## 构建 Go quota 二进制到 bin/quota
	cd services/go/quota && go build -o ../../bin/quota ./cmd

run-tenant:  ## 本地启动 tenant-svc（管理类，需要 PG + Redis）
	uvicorn tenant.main:app --reload --port 8005

run-admin:  ## 本地启动 admin-bff（聚合 + 审计，需要 PG + tenant-svc）
	uvicorn admin.main:app --reload --port 8006

run-docs:  ## 本地启动 docs-svc（OpenAPI 生成，需要 PG）
	uvicorn docs.main:app --reload --port 8007

run-trace:  ## 本地启动 trace-svc（CH 调用日志查询，需要 PG + ClickHouse）
	uvicorn trace_svc.main:app --reload --port 8008

run-retry:  ## 本地启动 retry-svc（失败重试，需要 Kafka + PG + Redis + executor）
	uvicorn retry_svc.main:app --reload --port 8009

run-workflow:  ## 本地启动 workflow-svc（Argo Workflow 封装，dev 默认 stub 模式）
	uvicorn workflow_svc.main:app --reload --port 8010

run-notification:  ## 本地启动 notification-svc（Webhook 推送，需要 PG + Kafka）
	uvicorn notification.main:app --reload --port 8012

run-ai-gateway:  ## 本地启动 ai-gateway（LLM 推理路由，需要 PG）
	uvicorn ai_gateway.main:app --reload --port 8013

run-billing:  ## 本地启动 billing-svc（出账 + 计费管理，需要 PG + CH）
	uvicorn billing.main:app --reload --port 8014

run-portal:  ## 本地启动 portal-bff（外部开发者门户聚合，需要 PG + auth）
	uvicorn portal.main:app --reload --port 8011

# ===== Admin Frontend (Vite + React) =====
admin-frontend-install:  ## 安装 admin 前端依赖（首次或 lockfile 变更后）
	cd frontend/admin && npm install

admin-frontend-typecheck:  ## 仅类型检查
	cd frontend/admin && npm run typecheck

admin-frontend-build:  ## 生产构建到 frontend/admin/dist（nginx 部署用）
	cd frontend/admin && npm run build

run-admin-frontend:  ## 本地启动 admin 前端 dev server（端口 5173，代理到本地各服务）
	cd frontend/admin && npm run dev -- --host

# ===== Portal Frontend (Vite + React) =====
portal-frontend-install:  ## 安装 portal 前端依赖（首次或 lockfile 变更后）
	cd frontend/portal && npm install

portal-frontend-typecheck:  ## 仅类型检查
	cd frontend/portal && npm run typecheck

portal-frontend-build:  ## 生产构建到 frontend/portal/dist
	cd frontend/portal && npm run build

run-portal-frontend:  ## 本地启动 portal 前端 dev server（端口 5174）
	cd frontend/portal && npm run dev -- --host

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

dev-up-multi:  ## 启动双区测试栈（2×PG/CH/Redis，region-labeled 端口）
	docker compose -f docker-compose.multi-region.yml up -d
	sleep 5
	@echo "pg-sh:15432 pg-bj:5433 redis-sh:6379 redis-bj:6380 ch-sh:127.0.0.2:18123 ch-bj:127.0.0.3:18123"

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

db-apply:  ## 幂等回放 init-db/*.sql 到运行中的 apihub-pg（dev/kind）
	bash scripts/k8s/apply-db.sh
