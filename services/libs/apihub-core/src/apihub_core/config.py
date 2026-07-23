"""服务配置基类（基于 pydantic-settings）。"""

import os
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
    # 启动建连退避重试（kind CNI/DNS 抢跑：首枪 EAI_AGAIN/连接拒绝时退避，
    # 在 startupProbe 窗口内建好，避免 CrashLoopBackOff）
    startup_connect_retries: int = 10
    startup_connect_backoff: float = 1.5
    # asyncpg ssl 值：disable / prefer / require / verify-ca / verify-full
    # dev 默认 prefer（先试 SSL，PG 未装 SSL 则回落明文，与 disable 行为一致但面向未来）；
    # prod 必须 require 或 verify-full（由 .env 显式覆盖）。
    pg_ssl: str = "prefer"

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

    # ClickHouse 双集群（跨 Region 查询用）
    # 多 Region 部署时，每个 Region 有独立 CH 集群。
    # trace-svc 读本地 CH（ch_host），但跨 Region 聚合查询时需通过 peer_region_ch_host
    # 访问对端 Region 的 CH，实现 unified trace dashboard。
    home_region: str = Field(default="sh", alias="HOME_REGION")
    """当前部署 Region（sh=cn-shanghai, bj=cn-beijing）。"""
    peer_region_ch_host: str | None = Field(default=None, alias="PEER_REGION_CH_HOST")
    """对端 Region 的 ClickHouse HTTP 地址（如 http://ch-sh.internal:8123）。
       空值表示单 Region 模式或暂无对端可查询。"""
    peer_region_pg_dsn: str | None = Field(default=None, alias="PEER_REGION_PG_DSN")
    """对端 Region PG DSN（逻辑订阅源/故障切换用，Python 侧可读）。"""
    peer_region_ch_user: str = Field(default="default", alias="PEER_REGION_CH_USER")
    peer_region_ch_password: str = Field(default="", alias="PEER_REGION_CH_PASSWORD")

    # Argo Workflow（workflow-svc 用）
    # stub：dev / test 内存模拟；k8s：in-cluster 走 K8s API
    argo_mode: str = "stub"
    k8s_api_server: str = "https://kubernetes.default.svc"
    argo_server_url: str = "https://argo-server.argo:2746"
    argo_server_insecure: bool = True  # dev：argo-server 自签证书；prod 改 False + 真 CA

    # OTel
    otel_exporter_otlp_endpoint: str | None = None
    otel_service_name: str = "apihub-service"
    otel_resource_attributes: str = ""

    # APISIX admin
    apisix_admin_url: str | None = None
    apisix_admin_key: str | None = None
    # 可信入口共享密钥：APISIX proxy-rewrite 注入 X-Ingress-Auth=<本值>，dispatcher 信任路径据此
    # 跳过 HTTP auth 回源。安全前提：dispatcher 仅经 APISIX 可达（ClusterIP，无外部 ingress）。
    ingress_shared_secret: str | None = None
    dispatcher_upstream: str = "dispatcher.apihub-system:80"

    # Auth service（鉴权调用）
    # K8s 默认走集群内 DNS；本地 dev 通过 .env.dev 覆盖到 localhost:8002
    auth_service_url: str = "http://auth.apihub-system/v1/apikey/verify"

    # JWT（外部开发者「人」的登录态；与机器 API Key 分流）
    # prod 必须用强密钥（env 注入），dev 默认值仅供本地。
    notification_service_url: str = "http://notification.apihub-system/v1/notification"
    jwt_secret: str = "dev-only-insecure-secret"  # noqa: S105  dev 默认占位，prod 由 env 注入强密钥
    jwt_ttl_seconds: int = 7200  # access token 2h
    jwt_refresh_ttl_seconds: int = 604800  # refresh token 7天

    # Admin SSO（钉钉 OAuth）—— admin 控制台浏览器登录。
    # 凭据可选：dingtalk_client_id 为空 → SSO 未启用（authorize/callback 返 503），
    # admin 仍可用 X-API-Key 机器访问（spec §10）。不加入 _INSECURE_DEFAULTS。
    dingtalk_client_id: str = ""
    dingtalk_client_secret: str = ""  # noqa: S105  prod 经 external-secrets 注入
    dingtalk_corp_id: str = ""
    dingtalk_sso_redirect_uri: str = "http://localhost:5173/login/callback"
    # dev/kind：mock IdP（免真实钉钉应用即可全链 e2e），同 argo_mode=stub 哲学。
    dingtalk_mock_mode: bool = False
    # 命中即置平台超管（仅 upsert 时设，不撤）。逗号分隔 unionId。
    bootstrap_admin_dingtalk_unionids: str = ""
    admin_jwt_ttl_seconds: int = 28800  # admin access token 8h

    # AI Gateway（ai-gateway 微服务用）
    ai_gateway_encryption_key: str = ""

    # HMAC 签名密钥加密 key（AES-256-GCM，32 字节 hex）——独立于 ai_gateway_encryption_key
    hmac_secret_key: str = ""
    # HMAC 请求验签参数
    hmac_timestamp_window_seconds: int = 300  # ±5min
    hmac_nonce_ttl_seconds: int = 600  # 10min
    # auth HMAC secret 冷路径（dispatcher 取 HMAC secret 用）
    hmac_secret_service_url: str = "http://auth.apihub-system/v1/internal/hmac-secret"  # noqa: S105

    # R3e: dispatcher 进程内 L1 缓存（identity/snapshot，TTL=5s 削峰；Redis 仍为真相源）
    dispatcher_l1_enabled: bool = True
    dispatcher_l1_ttl_seconds: float = 5.0
    dispatcher_l1_maxsize: int = 4096

    # PII 加密密钥（AES-256-GCM，32 字节 hex）
    pii_encryption_key: str = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"

    # OSS / MinIO（S3-compatible）
    oss_endpoint: str = "http://localhost:9000"
    oss_access_key: str = "apihub"
    oss_secret_key: str = "apihub_dev_pwd"  # noqa: S105  dev 占位（本地 MinIO），prod 由 env 注入
    oss_bucket_audit: str = "audit-archive"

    # 存储层安全
    oss_secure: bool = False

    # 下游服务 URL（BFF 聚合用）
    # K8s 默认走集群内 DNS；dev 在 .env.dev 覆盖到 localhost
    tenant_service_url: str = "http://tenant.apihub-system/v1/tenant"
    executor_service_template: str = (
        "http://executor.apihub-system.svc.cluster.local:{port}/v1/internal/retry"
    )
    # retry worker 调 executor 的端口（k8s 由 EXECUTOR_SERVICE_TEMPLATE 覆盖为无 {port} 时无效；
    # dev 本地回退到带 {port} 的 template 时生效）。默认 8003 = executor 本地端口。
    # validation_alias 避开 k8s service discovery 自动注入的 EXECUTOR_PORT=tcp://<clusterIP>:80
    # （命中该非 int 值会让 Settings 校验崩 → 所有服务启动失败）。改读 EXECUTOR_APP_PORT。
    executor_port: int = Field(default=8003, validation_alias="EXECUTOR_APP_PORT")
    # workflow-svc（dispatcher /v1/jobs 代理目标，文档 §4）
    # K8s 默认走集群内 DNS；dev 在 .env.dev 覆盖到 localhost:8010
    workflow_service_url: str = "http://workflow.apihub-system"

    def validate_security(self) -> None:
        """prod（或 REQUIRE_SECURE_SECRETS=1）禁止使用不安全默认密钥。

        dev/test 仍允许默认值，便于本地启动；prod 漏配即启动失败。
        """
        enforce = self.env.lower() == "prod" or os.environ.get("REQUIRE_SECURE_SECRETS") == "1"
        if not enforce:
            return
        bad = [k for k, v in _INSECURE_DEFAULTS.items() if getattr(self, k) == v]
        if bad:
            raise RuntimeError(
                f"Insecure default secrets in prod ({bad}); "
                "inject real values via env (jwt_secret/pii_encryption_key/oss_secret_key)."
            )


# 不安全默认值清单：prod 不允许使用（R0a §2.5）
_INSECURE_DEFAULTS = {
    "jwt_secret": "dev-only-insecure-secret",
    "pii_encryption_key": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
    "oss_secret_key": "apihub_dev_pwd",
    "hmac_secret_key": "",  # prod 必须注入，缺则启动 fail-closed
}


@lru_cache
def get_settings() -> Settings:
    s = Settings()  # type: ignore[call-arg]
    s.validate_security()
    return s
