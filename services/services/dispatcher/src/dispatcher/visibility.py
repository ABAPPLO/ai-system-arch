"""visibility 授权 —— dispatcher 转发前检查 api.visibility。

public: 任何有效 caller（含 external-public）。
tenant: 仅同租户。
private: 同租户 + 平台超管。
"""

from apihub_core.errors import ApiError, ErrorCode
from apihub_core.tenant import TenantContext


def check_visibility(snap, ctx: TenantContext) -> None:
    visibility = getattr(snap, "visibility", "private")
    api_tenant = snap.tenant_id

    if visibility == "public":
        return
    if visibility == "tenant":
        if ctx.tenant_id != api_tenant:
            raise ApiError(ErrorCode.FORBIDDEN, "api not visible to this tenant", http_status=403)
        return
    # private
    if ctx.tenant_id != api_tenant or not ctx.is_platform_admin:
        raise ApiError(ErrorCode.FORBIDDEN, "api is private", http_status=403)
