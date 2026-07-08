"""延迟队列 worker —— 轮询 Redis ZSet 到期任务，调用 executor 重试。

设计要点：
  - 单 worker 协程，周期 poll（默认 1s）
  - SCAN 多个 tenant key 分批处理
  - 调用 executor HTTP 接口（POST /v1/internal/exec）
  - 成功 → mark_succeeded + ZSet processing 移除
  - 失败 → mark_failed_attempt + 重新 push ZSet 或 进死信
"""

import asyncio
import contextlib
import json
import time

import httpx
from apihub_core.logging import get_logger
from apihub_core.tenant import TenantContext, clear_tenant_context, set_tenant_context

from retry_svc import delay_queue
from retry_svc import repository as repo
from retry_svc.backoff import next_attempt_delay_ms

log = get_logger(__name__)

# executor 内部 HTTP 接口（k8s 内网 DNS / dev 通过 .env.dev 覆盖到 localhost）
from apihub_core.config import get_settings  # noqa: E402

EXECUTOR_URL_TEMPLATE = get_settings().executor_service_template

POLL_INTERVAL_S = 1.0
BATCH_SIZE = 10
EXECUTOR_TIMEOUT_S = 30.0


class RetryWorker:
    """后台 worker：轮询延迟队列 + 调 executor。"""

    def __init__(
        self,
        *,
        executor_port: int = 8003,
        poll_interval_s: float = POLL_INTERVAL_S,
        batch_size: int = BATCH_SIZE,
    ):
        self._executor_url = EXECUTOR_URL_TEMPLATE.format(port=executor_port)
        self._poll_interval = poll_interval_s
        self._batch_size = batch_size
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._client: httpx.AsyncClient | None = None
        # 处理中的任务（防止同一 retry_task_id 重复 poll）
        self._inflight: set[int] = set()

    async def start(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=2.0, read=30.0, write=5.0, pool=2.0),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )
        self._task = asyncio.create_task(self._run(), name="retry-worker")
        log.info("worker_started", executor_url=self._executor_url)

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._task, timeout=30.0)
        if self._client:
            await self._client.aclose()
            self._client = None
        log.info("worker_stopped")

    async def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    await self._tick()
                except Exception as e:
                    log.exception("worker_tick_error", error=str(e))
                # 等下一轮（或被 stop 唤醒）
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self._poll_interval
                    )
        except asyncio.CancelledError:
            log.info("worker_cancelled")
            raise

    async def _tick(self) -> None:
        """一轮 poll：扫所有 tenant → 取到期任务 → 调 executor。"""
        try:
            tenants = await delay_queue.list_tenants_with_pending()
        except Exception as e:
            log.warning("delay_queue_scan_failed", error=str(e))
            return

        for tenant_id in tenants:
            if self._stop.is_set():
                break
            await self._process_tenant(tenant_id)

    async def _process_tenant(self, tenant_id: str) -> None:
        """处理单个 tenant 的到期任务。"""
        try:
            due_ids = await delay_queue.pop_due(
                tenant_id=tenant_id, max_count=self._batch_size
            )
        except Exception as e:
            log.warning("pop_due_failed", tenant_id=tenant_id, error=str(e))
            return

        if not due_ids:
            return

        set_tenant_context(TenantContext(
            tenant_id=tenant_id,
            tenant_type="internal",
            app_id="",
        ))
        try:
            for retry_task_id in due_ids:
                if self._stop.is_set():
                    break
                await self._execute_one(tenant_id, retry_task_id)
        finally:
            clear_tenant_context()

    async def _execute_one(self, tenant_id: str, retry_task_id: int) -> None:
        """执行单个 retry_task。"""
        # 状态机：pending → running（原子），False = 已被手动改状态，跳过
        try:
            won = await repo.mark_attempt_started(retry_task_id)
        except Exception as e:
            log.exception("mark_started_failed", retry_task_id=retry_task_id, error=str(e))
            await self._safe_complete(tenant_id, retry_task_id)
            return

        if not won:
            log.info("retry_skipped_not_pending", retry_task_id=retry_task_id)
            await self._safe_complete(tenant_id, retry_task_id)
            return

        # 读 detail 拿 original_request
        try:
            detail = await repo.get_retry_task(retry_task_id)
        except Exception as e:
            log.exception("get_retry_task_failed", retry_task_id=retry_task_id, error=str(e))
            await self._safe_complete(tenant_id, retry_task_id)
            return

        if detail is None:
            await self._safe_complete(tenant_id, retry_task_id)
            return

        result = await self._call_executor(detail)

        if result["succeeded"]:
            try:
                await repo.mark_succeeded(
                    retry_task_id,
                    response_status=result["status"],
                    response_body=result["body"],
                    latency_ms=result["latency_ms"],
                )
                log.info(
                    "retry_succeeded",
                    retry_task_id=retry_task_id,
                    attempt_no=detail.retry_count + 1,
                )
            except Exception as e:
                log.exception("mark_succeeded_failed", error=str(e))
        else:
            # 还有重试机会？
            next_attempt_no = detail.retry_count + 2  # 这次失败了，下一次
            if next_attempt_no > detail.max_attempts:
                # 进死信
                try:
                    await repo.mark_failed_attempt(
                        retry_task_id,
                        error_code=result.get("error_code") or "executor_failed",
                        error_msg=result.get("error_msg") or "",
                        response_status=result.get("status"),
                        response_body=result.get("body"),
                        latency_ms=result.get("latency_ms", 0),
                        next_retry_at=None,
                    )
                    log.warning(
                        "retry_dead_letter",
                        retry_task_id=retry_task_id,
                        attempts=detail.retry_count + 1,
                    )
                except Exception as e:
                    log.exception("mark_dead_failed", error=str(e))
            else:
                # 重新 push
                delay_ms = next_attempt_delay_ms(
                    detail.retry_count + 1,
                    policy=detail.backoff_policy,
                    base_ms=detail.backoff_base_ms,
                )
                from datetime import UTC, datetime, timedelta
                next_ts = time.time() + delay_ms / 1000.0
                next_retry_at = datetime.now(UTC) + timedelta(milliseconds=delay_ms)

                try:
                    await repo.mark_failed_attempt(
                        retry_task_id,
                        error_code=result.get("error_code") or "executor_failed",
                        error_msg=result.get("error_msg") or "",
                        response_status=result.get("status"),
                        response_body=result.get("body"),
                        latency_ms=result.get("latency_ms", 0),
                        next_retry_at=next_retry_at.replace(tzinfo=None),
                    )
                    await delay_queue.schedule(
                        tenant_id=tenant_id,
                        retry_task_id=retry_task_id,
                        next_attempt_at_ts=next_ts,
                    )
                    log.info(
                        "retry_rescheduled",
                        retry_task_id=retry_task_id,
                        next_attempt_no=next_attempt_no,
                        next_in_ms=delay_ms,
                    )
                except Exception as e:
                    log.exception("reschedule_failed", error=str(e))

        await self._safe_complete(tenant_id, retry_task_id)

    async def _call_executor(self, detail) -> dict:
        """POST /v1/internal/retry 到 executor。"""
        if self._client is None:
            return {"succeeded": False, "error_code": "worker_not_ready"}

        req = {
            "task_id": detail.original_request.get("task_id", ""),
            "backend_url": detail.original_request.get("backend_url", ""),
            "payload": detail.original_request.get("payload", ""),
            "tenant_id": str(detail.tenant_id),
            "api_id": str(detail.api_id),
            "app_id": str(detail.app_id),
            "trace_id": detail.trace_id,
            "request_id": detail.original_request.get("request_id", ""),
            "timeout_seconds": 30.0,
        }

        started = time.monotonic()
        try:
            resp = await self._client.post(
                self._executor_url,
                json=req,
                timeout=EXECUTOR_TIMEOUT_S,
            )
        except httpx.TimeoutException as e:
            return {
                "succeeded": False,
                "error_code": "executor_timeout",
                "error_msg": str(e),
                "latency_ms": int((time.monotonic() - started) * 1000),
            }
        except httpx.RequestError as e:
            return {
                "succeeded": False,
                "error_code": "executor_unreachable",
                "error_msg": str(e),
                "latency_ms": int((time.monotonic() - started) * 1000),
            }

        latency_ms = int((time.monotonic() - started) * 1000)
        body = {}
        with contextlib.suppress(json.JSONDecodeError, ValueError):
            body = resp.json()

        # executor 的 /v1/internal/retry 总是 HTTP 200，真正的成功/失败信号在 body["succeeded"]
        # 之前用 200<=status<300 判断会把"executor 调 backend 失败"误判成"重试成功"。
        if not isinstance(body, dict) or "succeeded" not in body:
            return {
                "succeeded": False,
                "status": resp.status_code,
                "body": body,
                "error_code": f"executor_bad_response_{resp.status_code}",
                "error_msg": (resp.text or "")[:500],
                "latency_ms": latency_ms,
            }
        return {
            "succeeded": bool(body.get("succeeded")),
            "status": body.get("status") or resp.status_code,
            "body": body.get("body") if isinstance(body.get("body"), (dict, list)) else {},
            "error_code": body.get("error_code"),
            "error_msg": body.get("error_msg") or "",
            "latency_ms": body.get("latency_ms") or latency_ms,
        }

    async def _safe_complete(self, tenant_id: str, retry_task_id: int) -> None:
        try:
            await delay_queue.complete(
                tenant_id=tenant_id, retry_task_id=retry_task_id
            )
        except Exception as e:
            log.warning("complete_failed", retry_task_id=retry_task_id, error=str(e))
