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

    # OTel
    otel_exporter_otlp_endpoint: str | None = None
    otel_service_name: str = "apihub-service"
    otel_resource_attributes: str = ""

    # APISIX admin
    apisix_admin_url: str | None = None
    apisix_admin_key: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
