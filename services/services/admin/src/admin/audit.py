"""审计 helper —— 把当前请求的元信息打包成 AuditRecord。

两类调用：
  1. 自动（middleware）：mutation 请求结束时调用 record_from_request
  2. 手动（业务代码）：构造 AuditRecord + repository.record

action 命名约定（动词_资源）：
  create_tenant / update_tenant / suspend_tenant / resume_tenant / close_tenant
  add_member / remove_member / update_member_role
  publish_api / unpublish_api / create_api_version / deprecate_api
  create_app / revoke_apikey / ...
"""

from typing import Any

from apihub_core.logging import get_logger
from apihub_core.tenant import get_tenant_context
from fastapi import Request

from admin import repository
from admin.models import AuditRecord

log = get_logger(__name__)


# 已知的 mutation 方法 → action 前缀
_METHOD_PREFIX = {
    "POST": "create",
    "PUT": "update",
    "PATCH": "update",
    "DELETE": "delete",
}

# 已知的路径前缀段（不算资源也不算 id）
_PREFIX_SEGS = {"v1", "v2", "admin", "internal", "api"}

# 已知的资源关键词（单复数 + 中划线变体）。新加资源时记得登记。
_KNOWN_RESOURCES = {
    "tenants",
    "tenant",
    "members",
    "member",
    "apps",
    "app",
    "apis",
    "api",
    "versions",
    "version",
    "api-keys",
    "api-key",
    "keys",
    "key",
    "tasks",
    "task",
    "audit",
    "quota",
    "usage",
    "children",
}

# 已知的 verb（出现在路径末尾的"动作"段，如 /tenants/{id}/suspend）
_KNOWN_VERBS = {
    "suspend",
    "resume",
    "close",
    "publish",
    "unpublish",
    "deprecate",
    "activate",
    "deactivate",
    "approve",
    "reject",
    "verify",
    "reset",
    "regenerate",
    "export",
    "import",
    "record",
    "record-batch",
}


def _infer_action(request: Request) -> tuple[str, str, str | None]:
    """从 path + method 推断 (action, resource_type, resource_id)。

    规则：
      - 取路径里最后出现的"资源关键词"作为 resource_type
      - 资源关键词后紧跟的段作为 resource_id
      - 路径末段如果是已知 verb，作为 action 前缀
      - 否则用 HTTP 方法推断（POST=create / PUT|PATCH=update / DELETE=delete）

    返回 ("", "", None) 表示不审计（GET / 健康 / 无已知资源 / 等）。
    """
    method = request.method.upper()
    if method not in _METHOD_PREFIX:
        return "", "", None

    path = request.url.path
    parts = [p for p in path.split("/") if p]

    # 跳过非业务路径
    if not parts or parts[0] in ("health", "metrics", "docs", "openapi.json"):
        return "", "", None

    # 找最深的资源关键词 + 紧跟的 id（如果有）
    resource_type: str | None = None
    resource_id: str | None = None
    for p in parts:
        if p in _PREFIX_SEGS:
            continue
        if p in _KNOWN_RESOURCES:
            resource_type = p
            resource_id = None
            continue
        if resource_type is not None and resource_id is None:
            # 跟在资源后的第一个非资源段 → id
            resource_id = p
            continue
        # resource_type 和 resource_id 都有了 → 后续段忽略（除最后一段可能是 verb）

    # 末段是不是 verb
    last = parts[-1] if parts else ""
    verb = last if last in _KNOWN_VERBS else None

    if not resource_type:
        return "", "", None

    prefix = _METHOD_PREFIX[method]
    action = f"{verb}_{resource_type}" if verb else f"{prefix}_{resource_type}"

    return action, resource_type, resource_id


async def record_from_request(
    request: Request,
    *,
    status_code: int = 200,
    extra_detail: dict[str, Any] | None = None,
) -> int:
    """从 FastAPI Request 自动构造 + 写审计。"""
    action, resource_type, resource_id = _infer_action(request)
    if not action:
        return 0

    ctx = get_tenant_context()
    if ctx is None:
        # 无租户上下文（如未鉴权的内部调用）→ 不审计，避免污染
        return 0

    detail = {"method": request.method, "path": str(request.url.path)}
    if status_code >= 400:
        detail["status_code"] = status_code
    if extra_detail:
        detail.update(extra_detail)

    entry = AuditRecord(
        tenant_id=ctx.tenant_id,
        actor_type="user" if ctx.user_id else ("app" if ctx.app_id else "system"),
        actor_id=ctx.user_id or ctx.app_id,
        actor_name=None,  # 由 auth 服务后续填
        actor_ip=request.client.host if request.client else None,
        auth_method="api_key",
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        env=None,
        detail=detail,
        user_agent=request.headers.get("user-agent"),
        request_id=request.headers.get("x-request-id"),
        trace_id=request.headers.get("x-trace-id"),
    )
    try:
        return await repository.record(entry)
    except Exception as e:
        log.warning("audit_record_from_request_failed", error=str(e))
        return 0
