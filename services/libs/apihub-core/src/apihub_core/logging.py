"""结构化日志（structlog + OTel trace 关联）。

详见 docs/08-observability-security.md §3.4
"""

import logging

import structlog
from opentelemetry import trace


def configure_logging(level: str = "INFO", env: str = "dev") -> None:
    """进程启动时调一次。"""
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
    )

    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _inject_trace,  # 把 OTel trace_id/span_id 注入每条日志
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if env == "dev":
        processors.append(structlog.dev.ConsoleRenderer(colors=True))
    else:
        processors.append(structlog.processors.JSONRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def _inject_trace(_, __, event_dict: dict) -> dict:
    """让每条日志自动带 trace_id / span_id。"""
    span = trace.get_current_span()
    ctx = span.get_span_context() if span else None
    if ctx and ctx.is_valid:
        event_dict["trace_id"] = f"{ctx.trace_id:032x}"
        event_dict["span_id"] = f"{ctx.span_id:016x}"
    return event_dict


def get_logger(name: str | None = None):
    return structlog.get_logger(name)


# 延迟 import sys 避免循环
import sys  # noqa: E402
