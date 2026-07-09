"""BFF 聚合 —— 用 httpx 调下游服务（tenant-svc 等）。

要点：
  - 走服务间 mTLS（K8s NetworkPolicy + ServiceAccount）
  - 透传 X-Request-Id / X-Tenant-Id / X-API-Key 头
  - 下游慢/挂 → 降级（返回 None + 告警），不要把 dashboard 拖死
  - 缓存 30s（dashboard 这类聚合查询）
"""

from typing import Any

import httpx
from apihub_core.config import get_settings
from apihub_core.logging import get_logger
from apihub_core.tenant import get_tenant_context

log = get_logger(__name__)


# 从 settings 读，dev 通过 .env.dev 覆盖到 localhost
_settings = get_settings()
TENANT_SVC_URL = _settings.tenant_service_url
AUTH_SVC_URL = _settings.auth_service_url.rsplit("/", 1)[0]  # /v1/apikey/verify -> /v1


class AggregatorClient:
    """聚合客户端 —— 包装 httpx.AsyncClient。"""

    def __init__(self) -> None:
        # 单连接池复用，timeout 短避免拖死 dashboard
        self._client: httpx.AsyncClient | None = None

    async def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(3.0, connect=1.0),
                limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get(
        self, url: str, *, headers: dict[str, str], params: dict[str, Any] | None = None
    ) -> dict | list | None:
        """GET 单个 URL，失败返回 None。"""
        c = await self.client()
        try:
            resp = await c.get(url, headers=headers, params=params)
            if resp.status_code != 200:
                log.warning(
                    "agg_downstream_error",
                    url=url,
                    status=resp.status_code,
                )
                return None
            return resp.json()
        except (httpx.HTTPError, Exception) as e:
            log.warning("agg_downstream_unreachable", url=url, error=str(e))
            return None

    async def list_tenants(
        self, *, api_key: str, parent_id: str | None = None
    ) -> list[dict[str, Any]]:
        """调 tenant-svc 列租户。失败 → []。"""
        headers = {"X-API-Key": api_key}
        params = {"parent_id": parent_id} if parent_id else None
        result = await self._get(f"{TENANT_SVC_URL}/tenants", headers=headers, params=params)
        if isinstance(result, list):
            return result
        return []

    async def get_tenant(self, *, api_key: str, tenant_id: str) -> dict[str, Any] | None:
        headers = {"X-API-Key": api_key}
        result = await self._get(f"{TENANT_SVC_URL}/tenants/{tenant_id}", headers=headers)
        return result if isinstance(result, dict) else None

    async def count_tenant_apps(self, *, api_key: str, tenant_id: str) -> int | None:
        """统计租户下 app 数（调 auth 的列表端点）。失败 → None。"""
        headers = {"X-API-Key": api_key}
        result = await self._get(
            f"{AUTH_SVC_URL}/tenants/{tenant_id}/apps",
            headers=headers,
        )
        if isinstance(result, list):
            return len(result)
        return None


# 进程级单例（FastAPI lifespan 里 init/close）
_aggregator: AggregatorClient | None = None


def get_aggregator() -> AggregatorClient:
    global _aggregator
    if _aggregator is None:
        _aggregator = AggregatorClient()
    return _aggregator


async def close_aggregator() -> None:
    global _aggregator
    if _aggregator is not None:
        await _aggregator.close()
        _aggregator = None


def _forward_headers(api_key: str) -> dict[str, str]:
    """透传当前请求的关键 header。"""
    ctx = get_tenant_context()
    headers: dict[str, str] = {"X-API-Key": api_key}
    if ctx:
        headers["X-Tenant-Id"] = ctx.tenant_id
        if ctx.user_id:
            headers["X-User-Id"] = ctx.user_id
    return headers
