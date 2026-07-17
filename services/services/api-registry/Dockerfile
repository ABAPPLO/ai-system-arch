# api-registry Dockerfile —— 多阶段构建
# 共享 base 镜像统一 Python 版本 + 系统依赖

# 构建上下文需要包含 apihub-core：在 CI 里先 copy libs/apihub-core 进来
#   docker build -f services/api-registry/Dockerfile \
#     -t registry.apihub.internal/apihub/api-registry:0.1.0-dev .

FROM python:3.11-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential libpq-dev libffi-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 apihub && chown -R apihub:apihub /app
USER apihub

COPY --chown=apihub:apihub services/libs/apihub-core /tmp/apihub-core
RUN pip install --user /tmp/apihub-core

COPY --chown=apihub:apihub services/services/api-registry/pyproject.toml /app/
COPY --chown=apihub:apihub services/services/api-registry/src /app/src
RUN pip install --user /app


FROM python:3.11-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
      libpq5 curl ca-certificates tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 1000 apihub

USER apihub
WORKDIR /app

COPY --from=builder --chown=apihub:apihub /home/apihub/.local /home/apihub/.local
COPY --from=builder --chown=apihub:apihub /app/src /app/src

ENV PATH="/home/apihub/.local/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "api_registry.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
