"""请求 / 响应模型。"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

# ---------- 审计记录 ----------


class AuditRecord(BaseModel):
    """内部服务调用 / 自动审计 middleware 写入。"""

    tenant_id: str = Field(min_length=1)
    actor_type: str = Field(default="user")  # user / app / system
    actor_id: str | None = None
    actor_name: str | None = None
    actor_ip: str | None = None
    auth_method: str | None = None  # api_key / sso / cookie
    action: str = Field(min_length=1)  # create_tenant / suspend_tenant / publish_api ...
    resource_type: str = Field(min_length=1)
    resource_id: str | None = None
    resource_name: str | None = None
    env: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    user_agent: str | None = None
    request_id: str | None = None
    trace_id: str | None = None


class AuditListItem(BaseModel):
    """列表项。"""

    id: int
    tenant_id: str
    actor_type: str
    actor_id: str | None
    actor_name: str | None
    action: str
    resource_type: str
    resource_id: str | None
    resource_name: str | None
    created_at: datetime


class AuditDetail(AuditListItem):
    """详情（含 detail/IP/UA）。"""

    actor_ip: str | None
    auth_method: str | None
    env: str | None
    detail: dict[str, Any] = Field(default_factory=dict)
    user_agent: str | None
    request_id: str | None
    trace_id: str | None


class AuditQuery(BaseModel):
    """查询参数（GET query string）。"""

    tenant_id: str | None = None  # 超管才能传非自己的
    actor_id: str | None = None
    action: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    limit: int = Field(default=50, ge=1, le=500)
    offset: int = Field(default=0, ge=0)


class AuditStats(BaseModel):
    """审计统计 —— 给 dashboard 用。"""

    total: int
    top_actions: list[dict[str, Any]] = Field(default_factory=list)
    top_actors: list[dict[str, Any]] = Field(default_factory=list)
    by_day: list[dict[str, Any]] = Field(default_factory=list)  # 最近 7/30 天


class RecordResponse(BaseModel):
    """record 端点的同步响应。"""

    id: int
    recorded: bool = True


# ---------- Dashboard ----------


class DashboardResponse(BaseModel):
    """跨服务聚合的概览。"""

    tenants: dict[str, Any] = Field(default_factory=dict)
    audit_today: int = 0
    audit_7d: int = 0
    top_recent_events: list[AuditListItem] = Field(default_factory=list)


# ---------- 审计归档 ----------


class ArchiveRequest(BaseModel):
    """归档请求。"""

    before: datetime | None = None
    """早于该时间的记录将被归档（默认 180 天前）。"""


class ArchiveResponse(BaseModel):
    """归档响应。"""

    archived: int
    cutoff: str


# ---------- 数据清理 ----------


class CleanupRequest(BaseModel):
    """数据清理请求。"""

    task_months: int | None = 12
    """task_instance 分区保留月数。"""
    retry_days: int | None = 30
    """retry_task 保留天数。"""


class CleanupResponse(BaseModel):
    """数据清理响应。"""

    dropped_partitions: int = 0
    deleted_retry_tasks: int = 0
