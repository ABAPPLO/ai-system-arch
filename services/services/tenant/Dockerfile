FROM python:3.11-slim AS builder

WORKDIR /app
ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential libpq-dev libffi-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 apihub && chown -R apihub:apihub /app
USER apihub

COPY --chown=apihub:apihub services/libs/apihub-core /tmp/apihub-core
RUN pip install --user /tmp/apihub-core

COPY --chown=apihub:apihub services/services/tenant/pyproject.toml /app/
COPY --chown=apihub:apihub services/services/tenant/src /app/src
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

EXPOSE 8005

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "tenant.main:app", "--host", "0.0.0.0", "--port", "8005", "--workers", "2"]
