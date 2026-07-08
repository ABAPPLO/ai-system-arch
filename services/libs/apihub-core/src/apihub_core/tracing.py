"""OpenTelemetry 初始化。

详见 docs/08-observability-security.md §5
"""

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.sdk.resources import DEPLOYMENT_ENVIRONMENT, SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from apihub_core.config import Settings


def configure_tracing(settings: Settings) -> None:
    if not settings.otel_exporter_otlp_endpoint:
        return

    resource_attrs = {
        SERVICE_NAME: settings.otel_service_name,
        DEPLOYMENT_ENVIRONMENT: settings.env,
    }
    for kv in settings.otel_resource_attributes.split(","):
        if "=" in kv:
            k, v = kv.split("=", 1)
            resource_attrs[k.strip()] = v.strip()

    resource = Resource.create(resource_attrs)
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint, insecure=True)
        )
    )
    trace.set_tracer_provider(provider)

    # 自动 instrumentation（OTel 0.40+ 把 instrument 改成实例方法，要带括号）
    # 注意：FastAPIInstrumentor().instrument() 只 patch 未来创建的 app；
    # 各服务的 app 已经在 create_app 里实例化完毕，必须走 instrument_app(app)（见 middleware.py）。
    # 这里仍然 instrument() 是为了兜底任何后来才 new 出来的 FastAPI 实例。
    FastAPIInstrumentor().instrument()
    HTTPXClientInstrumentor().instrument()
    AsyncPGInstrumentor().instrument()
    RedisInstrumentor().instrument()
