FROM python:3.11-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential libpq-dev libffi-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 apihub && chown -R apihub:apihub /app
USER apihub

COPY --chown=apihub:apihub services/libs/apihub-core /tmp/apihub-core
RUN pip install --user /tmp/apihub-core

COPY --chown=apihub:apihub services/services/auth/pyproject.toml /app/
COPY --chown=apihub:apihub services/services/auth/src /app/src
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

EXPOSE 8002

ENTRYPOINT ["/usr/bin/tini", "--"]
# 单 worker：auth 低 QPS（见 deployment.yaml resources 注释），多 worker 下 aiokafka
# producer 在 fork 子进程里 bootstrap Kafka 偶发失败（host.docker.internal 抖动 + fork race）
# → "Application startup failed" → CrashLoop（曾 RESTARTS 13）。HA 走 replicas，不走 worker 数。
CMD ["uvicorn", "auth.main:app", "--host", "0.0.0.0", "--port", "8002", "--workers", "1"]
