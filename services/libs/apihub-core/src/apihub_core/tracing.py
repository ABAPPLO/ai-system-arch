"""OpenTelemetry 初始化。

详见 docs/08-observability-security.md §5
"""

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource, SERVICE_NAME, DEPLOYMENT_ENVIRONMENT
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor

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

    # 自动 instrumentation
    FastAPIInstrumentor.instrument()
    HTTPXClientInstrumentor().instrument()
    AsyncPGInstrumentor().instrument()
    RedisInstrumentor().instrument()
