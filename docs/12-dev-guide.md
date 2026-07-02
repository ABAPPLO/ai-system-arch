# 12 · 本地开发指南

> `make dev-up` 一键拉起全套开发栈。

## 1. 前置依赖

| 工具 | 版本 | 用途 |
|------|------|------|
| Docker | 24+ | 跑 dev 栈 |
| docker-compose | v1.29+ 或 v2 | 编排 |
| Python | 3.11 | 业务服务 |
| make | any | 命令入口 |
| kubectl + kustomize | 1.28+ | 可选，对接 K8s |
| Terraform | 1.5+ | 可选，部署云资源 |

## 2. 一键启动

```bash
cp .env.dev.example .env.dev       # 首次复制
make install                       # 安装 Python 依赖
make dev-up                        # 拉起容器栈
```

启动后访问：

| 服务 | 地址 | 账号 |
|------|------|------|
| Grafana | http://localhost:3000 | admin / admin |
| Jaeger UI | http://localhost:16686 | - |
| MinIO Console | http://localhost:9001 | apihub / apihub_dev_pwd |
| ClickHouse HTTP | http://localhost:8123 | apihub / apihub_dev_pwd |
| Kafka (host) | localhost:9094 | - |
| PG | localhost:5432 | apihub / apihub_dev_pwd |
| Redis | localhost:6379 | apihub_dev_pwd |
| Prometheus | http://localhost:9090 | - |

## 3. 启动业务服务

dev 栈只跑基础设施，业务服务用 uvicorn 本地起（改代码自动 reload）：

```bash
# Terminal 1：跑 api-registry
make run-registry

# Terminal 2：调一下
curl -H "X-API-Key: ak_test_a_demo001" http://localhost:8000/v1/apis
```

## 4. 自检

### 4.1 PG RLS 验证

```bash
make dev-psql
```

```sql
-- 模拟 tenant_a 的请求
BEGIN;
SET LOCAL app.tenant_id = 'tenant_a';
SELECT id, name FROM api;       -- 只看到 api_demo_a 和 api_demo_llm
COMMIT;

-- 模拟 tenant_b
BEGIN;
SET LOCAL app.tenant_id = 'tenant_b';
SELECT id, name FROM api;       -- 只看到 api_demo_b
COMMIT;

-- 模拟超管
BEGIN;
SET LOCAL app.tenant_id = '';
SET LOCAL app.is_platform_admin = 'true';
SELECT tenant_id, count(*) FROM api GROUP BY tenant_id;
COMMIT;
```

### 4.2 ClickHouse 调用日志

```bash
curl 'http://localhost:8123/?user=apihub&password=apihub_dev_pwd' \
  --data-binary "SELECT tenant_id, count(), avg(latency_ms) FROM apihub.api_call_log GROUP BY tenant_id FORMAT PrettyCompact"
```

### 4.3 Kafka topic

```bash
docker exec -it apihub-kafka kafka-console-consumer.sh \
  --bootstrap-server localhost:9092 \
  --topic api-call-events \
  --from-beginning
```

### 4.4 OTel trace

业务代码上报后 → OTel Collector → Jaeger。

打开 http://localhost:16686，选 service = `api-registry`，查 trace。

## 5. 常用命令

| 命令 | 干啥 |
|------|------|
| `make dev-up` | 拉起容器栈 |
| `make dev-down` | 停容器栈（保 volume） |
| `make dev-logs` | tail -f 日志 |
| `make dev-ps` | 看容器状态 |
| `make dev-reset` | 清 volume 重建 |
| `make dev-psql` | 进 PG |
| `make dev-redis-cli` | 进 Redis |
| `make run-registry` | 本地跑 api-registry |
| `make test` | pytest |
| `make lint` | ruff + mypy |
| `make fmt` | ruff format + check --fix |

## 6. 排障

### 6.1 端口冲突

修改 `.env.dev` 里端口（如 `PG_PORT=15432`），相应改业务服务连接串。

### 6.2 ClickHouse 起不来

`ulimit -n` 太低。Linux：

```bash
sudo sh -c 'echo "fs.file-max = 262144" >> /etc/sysctl.conf'
sudo sysctl -p
```

### 6.3 Kafka KRaft 单节点模式

dev 栈用 KRaft（无 Zookeeper），replication_factor=1，不要在 dev 验证高可用。

### 6.4 OTel Collector 收不到 trace

检查业务服务的 `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317`。
容器内业务用 `http://otel-collector:4317`。

### 6.5 重置一切

```bash
make dev-reset
make install
make dev-up
```

## 7. 数据持久化

| Volume | 内容 | 是否随 dev-reset 清 |
|--------|------|---------------------|
| `pg-data` | PG 数据 | ✅ |
| `redis-data` | Redis AOF | ✅ |
| `kafka-data` | Kafka 日志 | ✅ |
| `clickhouse-data` | CH 数据 | ✅ |
| `minio-data` | MinIO 对象 | ✅ |
| `prometheus-data` | 指标（7 天） | ✅ |
| `grafana-data` | dashboard / 密码 | ✅ |

`make dev-down` 保留 volume；`make dev-reset` 清空。

## 8. 与 prod 的差异

| 维度 | dev | prod |
|------|-----|------|
| 部署 | docker compose 单机 | ACK 集群 |
| Kafka | KRaft 单 broker | 阿里云 Kafka 3 broker |
| PG | 单实例 | RDS 主备 + SQL 审计 |
| Redis | 单实例 | 集群版 8 分片 |
| OTel 采样 | 全量 | 错误/慢全采 + 1% 抽样 |
| 监控 | 共享 Grafana | 独立 |
