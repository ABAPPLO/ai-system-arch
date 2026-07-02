.PHONY: help install dev fmt lint test docker-build tf-init tf-plan tf-apply \
        k8s-apply-dev k8s-apply-staging k8s-apply-prod argocd-sync \
        run-registry run-dispatcher

SHELL := /bin/bash

ENV ?= dev
SERVICE ?= api-registry

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

run-dispatcher:  ## 本地启动 dispatcher（TODO）
	@echo "TODO: dispatcher 尚未实现"
