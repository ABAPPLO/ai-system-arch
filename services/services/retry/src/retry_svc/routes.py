"""retry-svc 路由 —— 失败任务后台管理 API。

权限：
  - 超管（is_platform_admin=True）：跨租户访问（通过 use_admin_session）
  - 普通用户：只看自己租户的 retry_task（RLS 自动过滤）

所有 GET 走 db_session（自动 RLS）。
POST trigger/ignore 也走 db_session，但内部需要切回 admin pool 才能
跨状态变 dead/ignored（手动操作不受 RLS 限制因为 retry_task 行已经
在本租户范围内）。
"""

from datetime import datetime

from apihub_core.errors import ApiError, ErrorCode
from apihub_core.tenant import require_tenant
from fastapi import FastAPI, Query

from retry_svc import repository as repo
from retry_svc.models import (
    ListFailedQuery,
    RetryStats,
    RetryStatus,
    RetryTaskDetail,
    RetryTaskRow,
)


def register_routes(app: FastAPI) -> None:
    # ⚠️ 路由声明顺序：静态段必须在 {param} 之前，否则 /health 会被吞
    @app.get("/v1/retry/health")
    async def health():
        return {"status": "ok", "service": "retry"}

    @app.get("/v1/retry/failed", response_model=list[RetryTaskRow])
    async def list_failed(
        since: datetime | None = None,
        until: datetime | None = None,
        api_id: str | None = None,
        app_id: str | None = None,
        status: RetryStatus | None = None,
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ):
        """失败任务列表。

        默认返回 pending / dead / ignored，可通过 status 精确过滤单个状态。
        """
        require_tenant()
        query = ListFailedQuery(
            since=since,
            until=until,
            api_id=api_id,
            app_id=app_id,
            status=status,
            limit=limit,
            offset=offset,
        )
        return await repo.list_failed(query)

    @app.get("/v1/retry/stats", response_model=RetryStats)
    async def get_stats():
        """重试统计 —— 各状态计数 + 成功率 + top error code。"""
        require_tenant()
        data = await repo.stats()
        return RetryStats(**data)

    @app.get("/v1/retry/{retry_task_id}", response_model=RetryTaskDetail)
    async def get_detail(retry_task_id: int):
        """单次重试详情（含全部 attempt 历史）。"""
        require_tenant()
        row = await repo.get_retry_task(retry_task_id)
        if row is None:
            raise ApiError(
                ErrorCode.NOT_FOUND,
                f"retry_task {retry_task_id} not found",
                http_status=404,
            )
        return row

    @app.post("/v1/retry/{retry_task_id}/trigger")
    async def trigger_retry(retry_task_id: int):
        """手动触发重试。

        dead / ignored / pending → pending + next_retry_at=NOW()。
        worker 下一轮 poll 会取到。
        """
        require_tenant()
        ok, _tenant = await repo.requeue_for_retry(retry_task_id)
        if not ok:
            raise ApiError(
                ErrorCode.NOT_FOUND,
                f"retry_task {retry_task_id} not found or not in triggerable state",
                http_status=404,
            )
        return {"retry_task_id": retry_task_id, "status": "pending"}

    @app.post("/v1/retry/{retry_task_id}/ignore")
    async def ignore_retry(retry_task_id: int):
        """标记忽略 —— 不再自动重试。"""
        require_tenant()
        ok = await repo.mark_ignored(retry_task_id)
        if not ok:
            raise ApiError(
                ErrorCode.NOT_FOUND,
                f"retry_task {retry_task_id} not found or not in ignorable state",
                http_status=404,
            )
        return {"retry_task_id": retry_task_id, "status": "ignored"}
