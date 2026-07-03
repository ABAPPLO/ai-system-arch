FROM python:3.11-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential libpq-dev libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY services/libs/apihub-core /tmp/apihub-core
RUN pip install --user /tmp/apihub-core

COPY services/services/trace/pyproject.toml /app/
COPY services/services/trace/src /app/src
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

EXPOSE 8008

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "trace_svc.main:app", "--host", "0.0.0.0", "--port", "8008", "--workers", "2"]
