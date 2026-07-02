# 共享 Python 基础镜像 — 减少服务 Dockerfile 重复
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 系统依赖：libpq（asyncpg）、libssl、curl（健康检查）
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential \
      curl \
      ca-certificates \
      libpq-dev \
      libssl-dev \
      libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# 非 root 用户
RUN useradd -m -u 1000 -s /bin/bash apihub
USER apihub
WORKDIR /app

# 安装 apihub-core 共享库（一次性，所有服务复用）
COPY --chown=apihub:apihub libs/apihub-core /tmp/apihub-core
RUN pip install --user /tmp/apihub-core && rm -rf /tmp/apihub-core

ENV PATH="/home/apihub/.local/bin:${PATH}"
