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


async def publish_route(
    *,
    version_id: str,
    method: str,
    path: str,
    base_path: str,
    rate_limit: dict | None = None,
) -> None:
    """upsert 一条 APISIX 路由（id=version_id）→ dispatcher + key-auth + 限流 + 注入 header。

    - key-auth：edge 校验 X-API-Key（无效→401，不到 dispatcher）。
    - limit-count：仅当 rate_limit（{count, window_seconds}）有 count 时加。
    - proxy-rewrite：注入 X-API-Version-Id（dispatcher 强制）；若 ingress_shared_secret 配置，
      同步注入 X-Ingress-Auth（dispatcher 据此走信任路径，跳过 HTTP auth 回源）。
    """
    settings = get_settings()
    if not settings.apisix_admin_url:
        raise ApiError(ErrorCode.INTERNAL, "APISIX_ADMIN_URL not configured", http_status=500)

    uri = (base_path.rstrip("/") + _normalize_path(path)) if base_path else _normalize_path(path)
    set_headers = {"X-API-Version-Id": version_id}
    if settings.ingress_shared_secret:
        set_headers["X-Ingress-Auth"] = settings.ingress_shared_secret

    plugins: dict = {
        "key-auth": {"header": "X-API-Key"},
        "proxy-rewrite": {
            "regex_uri": ["^/(.*)$", "/dispatch/$1"],
            "headers": {"set": set_headers},
        },
        # 启用 write-affinity：插件读 ctx.consumer.labels.home_region（S1-T1/T2/T3 已 wire），
        # 把写请求粘到 home region，覆盖所有 published 路由（R3b 承重墙）。
        "tenant-affinity": {},
    }
    if rate_limit and rate_limit.get("count"):
        plugins["limit-count"] = {
            "count": int(rate_limit["count"]),
            "time_window": int(rate_limit.get("window_seconds", 60)),
            "key": "consumer_name",
            "policy": "local",
            "rejected_code": 429,
        }

    body = {
        "uri": uri,
        "methods": [method.upper()],
        "upstream": {"type": "roundrobin", "nodes": {settings.dispatcher_upstream: 1}},
        "plugins": plugins,
    }
    await _admin_request(
        "PUT",
        f"{settings.apisix_admin_url}/apisix/admin/routes/{version_id}",
        json=body,
    )


async def upsert_consumer(*, key_id: str, key: str, labels: dict[str, str] | None = None) -> None:
    """upsert APISIX consumer（username=key_id，per-key）—— 随 APIKey 生命周期。

    consumer 持 key-auth 凭证（key=明文，header=X-API-Key），APISIX 在网关层秒级校验。
    per-key（非 per-app）：APISIX key-auth consumer 只能持一个 key，per-app 会让同 app
    第 2 个 key 覆盖第 1 个。consumer_name 对下游不透明（信任路径走 Redis，不读它）。

    labels：可选 consumer 标签（如 {"home_region": "bj"}），供 tenant-affinity 插件读取。
    """
    settings = get_settings()
    if not settings.apisix_admin_url:
        raise ApiError(ErrorCode.INTERNAL, "APISIX_ADMIN_URL not configured", http_status=500)
    body: dict = {
        "username": key_id,
        "plugins": {"key-auth": {"key": key, "header": "X-API-Key"}},
    }
    if labels:
        body["labels"] = labels
    await _admin_request(
        "PUT",
        f"{settings.apisix_admin_url}/apisix/admin/consumers/{key_id}",
        json=body,
    )


async def delete_consumer(key_id: str) -> None:
    """删 APISIX consumer（随 key 吊销）。不存在静默（404 不当错）。"""
    settings = get_settings()
    if not settings.apisix_admin_url:
        return  # 未配 APISIX（dev 无 APISIX 时 no-op）
    try:
        await _admin_request(
            "DELETE",
            f"{settings.apisix_admin_url}/apisix/admin/consumers/{key_id}",
        )
    except ApiError as e:
        # 404 = consumer 本就不存在（revoke 幂等），静默；其余（5xx/网络）抛出
        if "failed: 404" not in str(e):
            raise


async def retire_route(version_id: str) -> None:
    """占位 —— R1c 设计：retire 不删路由（dispatcher 按 retired 状态返 410）。

    保留供后续 stale 路由清理 follow-up 使用；R1c 内 retire handler 不调用本函数。
    """
    return None
