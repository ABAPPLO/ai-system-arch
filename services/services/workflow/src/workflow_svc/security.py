"""workflow 提交输入的安全校验 —— namespace 白名单 + spec 危险字段拒绝。

用户提交的 Argo Workflow spec 会被原样包成 CRD 递给 K8s API，这里在做
信任边界收口：namespace 只能落在配置白名单里，spec 里任何位置的
privileged / hostNetwork / hostPID / hostIPC / hostPath 一律拒绝。
deny-list 不是完整沙箱（完整隔离靠 namespace 内的 PSA/NetPol），
但堵住了「提交即拿节点权限」的最短提权路径。
"""

import re

from apihub_core.config import get_settings
from apihub_core.errors import ApiError, ErrorCode

_NS_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")

# 任何层级出现且为真值即拒绝的布尔型危险字段
_DANGEROUS_BOOL_KEYS = ("privileged", "hostNetwork", "hostPID", "hostIPC")
# volume 类型字段：出现 hostPath volume 即拒绝（挂宿主机路径）
_DANGEROUS_VOLUME_KEYS = ("hostPath",)


def validate_namespace(namespace: str) -> None:
    """namespace 必须在白名单内（argo_allowed_namespaces，逗号分隔）。"""
    if not namespace or not _NS_RE.match(namespace):
        raise ApiError(
            ErrorCode.INVALID_INPUT, f"invalid namespace: {namespace!r}", http_status=400
        )
    allowed = {ns.strip() for ns in get_settings().argo_allowed_namespaces.split(",") if ns.strip()}
    if namespace not in allowed:
        raise ApiError(
            ErrorCode.FORBIDDEN,
            f"namespace not allowed (allowed: {sorted(allowed)})",
        )


def _walk(node: object) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            if k in _DANGEROUS_BOOL_KEYS and v:
                raise ApiError(ErrorCode.FORBIDDEN, f"field not allowed in workflow spec: {k}")
            if k in _DANGEROUS_VOLUME_KEYS:
                raise ApiError(ErrorCode.FORBIDDEN, f"volume type not allowed: {k}")
            _walk(v)
    elif isinstance(node, list):
        for item in node:
            _walk(item)


def validate_spec_safety(spec: dict) -> None:
    """拒绝 spec 任意嵌套位置的节点级逃逸字段（privileged/hostPath 等）。"""
    if not isinstance(spec, dict):
        raise ApiError(ErrorCode.INVALID_INPUT, "spec must be an object", http_status=400)
    _walk(spec)
