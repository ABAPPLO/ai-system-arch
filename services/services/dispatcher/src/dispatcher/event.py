"""构造调用事件 payload —— 投递到 Kafka api-call-events。

ClickHouse 端 schema 与此对齐（见 scripts/init-clickhouse/01-schema.sql）。
"""

import uuid
from datetime import UTC, datetime

from apihub_core.events import CallEvent
from apihub_core.tenant import get_tenant_context
from opentelemetry import trace


def build_call_event(
    *,
    api_id: str,
    api_version_id: str,
    method: str,
    path: str,
    status_code: int,
    is_success: bool,
    latency_ms: int,
    request_size: int,
    response_size: int,
    backend_type: str = "http",
    backend_latency_ms: int = 0,
    error_code: str = "",
    error_msg: str = "",
    user_agent: str = "",
    client_ip: str = "0.0.0.0",
    ai_model: str = "",
    ai_streaming: bool = False,
    token_prompt: int = 0,
    token_completion: int = 0,
    token_total: int = 0,
    trace_id: str = "",
    request_id: str = "",
) -> CallEvent:
    """组装一条调用事件。

    tenant_id / tenant_type / app_id 由 apihub_core.kafka.emit 自动从
    contextvar 注入到 Kafka header（不重复写到 payload）。
    """
    ctx = get_tenant_context()
    tenant_id = ctx.tenant_id if ctx else ""
    tenant_type = ctx.tenant_type if ctx else ""
    app_id = ctx.app_id if ctx else ""

    d = {
        "ts": _now_ch_ts(),
        "tenant_id": tenant_id,
        "tenant_type": tenant_type,
        "app_id": app_id,
        "api_id": api_id,
        "api_version_id": api_version_id,
        "trace_id": trace_id or _otel_trace_id(),
        "request_id": request_id or _gen_request_id(),
        "method": method.upper(),
        "path": path,
        "status_code": status_code,
        "is_success": 1 if is_success else 0,
        "latency_ms": latency_ms,
        "request_size": request_size,
        "response_size": response_size,
        "error_code": error_code,
        "error_msg": error_msg,
        "user_agent": user_agent,
        "client_ip": client_ip,
        "backend_type": backend_type,
        "backend_latency_ms": backend_latency_ms,
        "ai_model": ai_model,
        "ai_streaming": 1 if ai_streaming else 0,
        "token_prompt": token_prompt,
        "token_completion": token_completion,
        "token_total": token_total,
    }
    return CallEvent(**d)  # type: ignore


def new_request_id() -> str:
    return _gen_request_id()


def _gen_request_id() -> str:
    return f"req_{uuid.uuid4().hex[:16]}"


def _gen_trace_id() -> str:
    return f"trc_{uuid.uuid4().hex[:16]}"


def _otel_trace_id() -> str:
    """优先用 OTel 当前 span 的 trace_id（= Jaeger 同一条 trace），无有效 span 才回落随机。

    R1b §3.9：让 ClickHouse 调用日志的 trace_id 能 join Jaeger。
    回落仍用 _gen_trace_id()（保留 trc_ 前缀，兼容 test_event 的断言）。
    """
    span = trace.get_current_span()
    ctx = span.get_span_context() if span is not None else None
    if ctx is not None and ctx.is_valid:
        return f"{ctx.trace_id:032x}"
    return _gen_trace_id()


def _now_ch_ts() -> str:
    """UTC now as ClickHouse DateTime64(3)-compatible string: 'YYYY-MM-DD HH:MM:SS.mmm'。

    CH JSONEachRow 解析 DateTime64 不认 ISO-8601（带 T / 时区偏移）→ 整行被判为解析错误、
    所有列落 default（见 phase2-findings「K8s 联调」CH Kafka-engine MV 条）。
    """
    n = datetime.now(UTC)
    return n.strftime("%Y-%m-%d %H:%M:%S.") + f"{n.microsecond // 1000:03d}"
