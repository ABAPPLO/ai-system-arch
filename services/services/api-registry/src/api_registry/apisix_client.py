"""APISIX Admin API 客户端 —— publish 时 upsert 路由。

路由策略：每 published api_version 一条 APISIX route（id=version_id），
upstream 指向 dispatcher，proxy-rewrite 注入 X-API-Version-Id + 把 path 重写成 /dispatch/...。
"""

import httpx
from apihub_core.config import get_settings
from apihub_core.errors import ApiError, ErrorCode


def _normalize_path(path: str) -> str:
    """{var} → APISIX :var 段匹配。"""
    out = []
    for seg in path.strip("/").split("/"):
        if seg.startswith("{") and seg.endswith("}"):
            out.append(":" + seg[1:-1])
        else:
            out.append(seg)
    return "/" + "/".join(out)


async def _admin_request(method: str, url: str, **kw) -> httpx.Response:
    settings = get_settings()
    headers = kw.pop("headers", {})
    headers["X-API-KEY"] = settings.apisix_admin_key or ""
    async with httpx.AsyncClient(timeout=3.0) as c:
        try:
            resp = await c.request(method, url, headers=headers, **kw)
        except httpx.RequestError as e:
            raise ApiError(
                ErrorCode.INTERNAL,
                f"apisix admin unreachable: {type(e).__name__}: {e!r}",
                http_status=502,
            ) from e
    if resp.status_code < 200 or resp.status_code >= 300:
        raise ApiError(
            ErrorCode.INTERNAL,
            f"apisix admin {method} {url} failed: {resp.status_code} {resp.text[:200]}",
            http_status=502,
        )
    return resp


async def publish_route(*, version_id: str, method: str, path: str, base_path: str) -> None:
    """upsert 一条 APISIX 路由（id=version_id）→ dispatcher + 注入 X-API-Version-Id。"""
    settings = get_settings()
    if not settings.apisix_admin_url:
        raise ApiError(ErrorCode.INTERNAL, "APISIX_ADMIN_URL not configured", http_status=500)

    uri = (base_path.rstrip("/") + _normalize_path(path)) if base_path else _normalize_path(path)
    body = {
        "uri": uri,
        "methods": [method.upper()],
        "upstream": {"type": "roundrobin", "nodes": {settings.dispatcher_upstream: 1}},
        "plugins": {
            "proxy-rewrite": {
                "regex_uri": ["^/(.*)$", "/dispatch/$1"],
                "headers": {"set": [f"X-API-Version-Id: {version_id}"]},
            }
        },
    }
    await _admin_request(
        "PUT",
        f"{settings.apisix_admin_url}/apisix/admin/routes/{version_id}",
        json=body,
    )


async def retire_route(version_id: str) -> None:
    """占位 —— R1c 设计：retire 不删路由（dispatcher 按 retired 状态返 410）。

    保留供后续 stale 路由清理 follow-up 使用；R1c 内 retire handler 不调用本函数。
    """
    return None
