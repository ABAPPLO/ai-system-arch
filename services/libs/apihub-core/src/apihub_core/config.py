"""服务配置基类（基于 pydantic-settings）。"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "apihub-service"
    env: str = Field(default="dev")
    log_level: str = Field(default="INFO")

    # PostgreSQL
    pg_host: str
    pg_port: int = 5432
    pg_database: str = "apihub"
    pg_user: str
    pg_password: str
    pg_pool_min: int = 10
    pg_pool_max: int = 50
    # asyncpg ssl 值：disable / prefer / require / verify-ca / verify-full
    # dev 默认 disable（容器内 PG 没装 SSL）；prod 必须 require 或 verify-full
    pg_ssl: str = "disable"

    # Redis
    redis_host: str
    redis_port: int = 6379
    redis_password: str | None = None
    redis_ssl: bool = False

    # Kafka
    kafka_brokers: str = Field(default="")

    # ClickHouse（trace-svc / analyzer 用）
    ch_host: str | None = None
    ch_port: int = 8123
    ch_username: str = "default"
    ch_password: str = ""
    ch_database: str = "apihub"
    ch_pool_size: int = 10

    # Argo Workflow（workflow-svc 用）
    # stub：dev / test 内存模拟；k8s：in-cluster 走 K8s API
    argo_mode: str = "stub"
    k8s_api_server: str = "https://kubernetes.default.svc"

    # OTel
    otel_exporter_otlp_endpoint: str | None = None
    otel_service_name: str = "apihub-service"
    otel_resource_attributes: str = ""

    # APISIX admin
    apisix_admin_url: str | None = None
    apisix_admin_key: str | None = None

    # Auth service（鉴权调用）
    # K8s 默认走集群内 DNS；本地 dev 通过 .env.dev 覆盖到 localhost:8002
    auth_service_url: str = "http://auth.apihub-system/v1/apikey/verify"

    # 下游服务 URL（BFF 聚合用）
    # K8s 默认走集群内 DNS；dev 在 .env.dev 覆盖到 localhost
    tenant_service_url: str = "http://tenant.apihub-system/v1/tenant"
    executor_service_template: str = (
        "http://executor.apihub-system.svc.cluster.local:{port}/v1/internal/retry"
    )
    # workflow-svc（dispatcher /v1/jobs 代理目标，文档 §4）
    # K8s 默认走集群内 DNS；dev 在 .env.dev 覆盖到 localhost:8010
    workflow_service_url: str = "http://workflow.apihub-system"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
