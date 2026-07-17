FROM python:3.11-slim AS builder
WORKDIR /app
ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn
RUN apt-get update && apt-get install -y --no-install-recommends build-essential libpq-dev libffi-dev zlib1g-dev && rm -rf /var/lib/apt/lists/*
RUN useradd -m -u 1000 apihub && chown -R apihub:apihub /app
USER apihub
COPY --chown=apihub:apihub services/libs/apihub-core /tmp/apihub-core
RUN pip install --user /tmp/apihub-core
COPY --chown=apihub:apihub services/services/ai-gateway /tmp/ai-gateway
RUN pip install --user /tmp/ai-gateway
FROM python:3.11-slim
RUN useradd -m -u 1000 apihub
USER apihub
WORKDIR /app
COPY --from=builder --chown=apihub:apihub /home/apihub/.local /home/apihub/.local
ENV PATH="/home/apihub/.local/bin:${PATH}"
EXPOSE 8013
CMD ["uvicorn", "ai_gateway.main:app", "--host", "0.0.0.0", "--port", "8013", "--workers", "1"]
