"""任务处理 —— 调业务后端 + 更新 PG 状态机。

幂等保证：先 mark_running（pending→running 原子转换），失败则跳过。
这是处理 Kafka at-least-once 重投的关键。
"""

import contextlib
import time

import httpx
from apihub_core import kafka
from apihub_core.events import TaskFailure, TaskRequest, TaskStatus
from apihub_core.logging import get_logger
from apihub_core.tenant import TenantContext, clear_tenant_context, set_tenant_context

from executor import repository as repo
from executor.models import TaskResult

log = get_logger(__name__)

# 进程级单例，所有任务复用连接池
_client: httpx.AsyncClient | None = None


async def init_http_client() -> None:
    """进程启动时调一次。"""
    global _client
    _client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=2.0, read=30.0, write=5.0, pool=2.0),
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
        http2=False,
    )


async def close_http_client() -> None:
    global _client
    if _client:
        await _client.aclose()
        _client = None


async def process_task(msg: TaskRequest) -> TaskResult:
    """处理单个任务。

    流程：
      1. 幂等：mark_running 原子 pending→running，False = 已被其他 worker 抢走 → 跳过
      2. 设 TenantContext（让 kafka.emit / 日志带 tenant_id）
      3. POST 到 backend_url
      4. 按 HTTP status / 异常映射到 succeeded/failed/timeout
      5. 更新 PG + 推 task-status 事件（fire-and-forget）
    """
    tenant_id = msg.tenant_id or ""

    won = await repo.mark_running(msg.task_id)
    if not won:
        log.info("task_skipped_already_running", task_id=msg.task_id)
        return TaskResult(task_id=msg.task_id, status="skipped")

    if tenant_id:
        set_tenant_context(
            TenantContext(
                tenant_id=tenant_id,
                tenant_type="internal",
                app_id=msg.app_id or "",
            )
        )

    try:
        result = await _call_backend(msg)
    finally:
        if tenant_id:
            clear_tenant_context()

    if result.status == "succeeded":
        await repo.mark_succeeded(
            msg.task_id,
            response_body=result.response_body or "",
            http_status=result.http_status or 200,
        )
    else:
        await repo.mark_failed(
            msg.task_id,
            error_code=result.error_code or "unknown",
            error_msg=result.error_msg or "",
            http_status=result.http_status,
        )

    async with _suppress_kafka_err():
        await kafka.emit_event(
            TaskStatus(
                task_id=msg.task_id,
                tenant_id=tenant_id,
                app_id=msg.app_id or "",
                api_id=msg.api_id,
                status=result.status,
                error_code=result.error_code or "",
                duration_ms=result.duration_ms,
                request_id=msg.request_id or "",
            )
        )
        if result.status != "succeeded":
            await kafka.emit_event(
                TaskFailure(
                    task_id=msg.task_id,
                    tenant_id=tenant_id,
                    app_id=msg.app_id or "",
                    api_id=msg.api_id,
                    api_version_id=msg.api_version_id,
                    backend_url=msg.backend_url,
                    trace_id=msg.trace_id or msg.task_id,
                    request_id=msg.request_id or "",
                    payload=msg.payload,
                    error_code=result.error_code or "unknown",
                    error_msg=(result.error_msg or "")[:5000],
                    timeout_seconds=msg.timeout_seconds,
                )
            )

    log.info(
        "task_processed",
        task_id=msg.task_id,
        status=result.status,
        http_status=result.http_status,
        duration_ms=result.duration_ms,
    )
    return result


async def _call_backend(msg: TaskRequest) -> TaskResult:
    """POST backend。所有异常都转成 TaskResult，不向上抛。"""
    if _client is None:
        return TaskResult(
            task_id=msg.task_id,
            status="failed",
            error_code="http_client_not_init",
            error_msg="executor http client not initialized",
        )

    started = time.monotonic()
    headers = {
        "Content-Type": "application/json",
        "X-Task-Id": msg.task_id,
        "X-Request-Id": msg.request_id or "",
        "X-Tenant-Id": msg.tenant_id or "",
        "X-Trace-Id": msg.trace_id or "",
    }
    # W3C traceparent：把当前 OTel context（由 consume_with_trace attach）
    # 注入 header，让 OTel 链延续到业务 backend（与既有 X-Trace-Id 共存）。
    from opentelemetry import propagate

    tp: dict[str, str] = {}
    propagate.inject(tp)
    headers.update(tp)

    try:
        resp = await _client.post(
            msg.backend_url,
            content=msg.payload.encode("utf-8") if msg.payload else b"",
            headers=headers,
            timeout=msg.timeout_seconds,
        )
    except httpx.TimeoutException as e:
        return TaskResult(
            task_id=msg.task_id,
            status="timeout",
            error_code="backend_timeout",
            error_msg=f"{type(e).__name__}: {e}",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    except httpx.RequestError as e:
        return TaskResult(
            task_id=msg.task_id,
            status="failed",
            error_code="backend_unreachable",
            error_msg=f"{type(e).__name__}: {e}",
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    duration_ms = int((time.monotonic() - started) * 1000)

    if 200 <= resp.status_code < 300:
        return TaskResult(
            task_id=msg.task_id,
            status="succeeded",
            http_status=resp.status_code,
            response_body=resp.text,
            duration_ms=duration_ms,
        )

    return TaskResult(
        task_id=msg.task_id,
        status="failed",
        http_status=resp.status_code,
        error_code=f"backend_http_{resp.status_code}",
        error_msg=resp.text[:500],  # 截断，避免巨型错误写爆 PG
        duration_ms=duration_ms,
    )


class _suppress_kafka_err(contextlib.AbstractAsyncContextManager):
    """Kafka 推送失败不能影响 task 主流程（已经写好 PG 了）。"""

    async def __aexit__(self, exc_type, exc, tb):
        if exc:
            log.warning("kafka_emit_failed", error=str(exc))
        return True  # swallow
