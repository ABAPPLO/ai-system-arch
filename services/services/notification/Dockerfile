FROM python:3.11-slim AS builder
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends build-essential libpq-dev libffi-dev zlib1g-dev && rm -rf /var/lib/apt/lists/*
RUN useradd -m -u 1000 apihub && chown -R apihub:apihub /app
USER apihub
COPY --chown=apihub:apihub services/libs/apihub-core /tmp/apihub-core
RUN pip install --user /tmp/apihub-core
COPY --chown=apihub:apihub services/services/notification /tmp/notification
RUN pip install --user /tmp/notification
FROM python:3.11-slim
WORKDIR /app
COPY --from=builder /home/apihub/.local /root/.local
ENV PATH=/root/.local/bin:$PATH
EXPOSE 8012
CMD ["uvicorn", "notification.main:app", "--host", "0.0.0.0", "--port", "8012", "--workers", "2"]
