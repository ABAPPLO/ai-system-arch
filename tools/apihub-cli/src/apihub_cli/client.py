"""api-registry REST 客户端 —— 给 CLI 用。

不依赖 apihub_core（CLI 是独立工具，跑在开发者机器或 CI 容器里）。
"""

from typing import Any

import httpx


class RegistryError(Exception):
    """api-registry 返回非 2xx。"""

    def __init__(self, status: int, payload: dict[str, Any] | str):
        self.status = status
        self.payload = payload
        super().__init__(f"api-registry {status}: {payload}")


class RegistryClient:
    """封装 api-registry 的 4 个核心端点。"""

    def __init__(self, base_url: str, api_key: str, *, timeout: float = 10.0):
        self._base = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self._base,
            headers={
                "X-API-Key": api_key,
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "RegistryClient":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    # ----- API -----

    def list_apis(self, *, limit: int = 200) -> list[dict[str, Any]]:
        r = self._client.get("/v1/apis", params={"limit": limit})
        return self._unwrap(r)["items"]

    def find_api_by_name(self, name: str) -> dict[str, Any] | None:
        """api-registry 没有 by-name 查询接口，本地过滤。
        名称是业务侧唯一键（CLAUDE/ADR 锁定）。
        """
        for api in self.list_apis():
            if api.get("name") == name:
                return api
        return None

    def create_api(self, payload: dict[str, Any]) -> str:
        r = self._client.post("/v1/apis", json=payload)
        data = self._unwrap(r)
        return data["api_id"]

    def create_version(self, payload: dict[str, Any]) -> dict[str, Any]:
        r = self._client.post("/v1/api-versions", json=payload)
        return self._unwrap(r)

    def submit_change_request(
        self, payload: dict[str, Any],
    ) -> dict[str, Any]:
        r = self._client.post("/v1/change-requests", json=payload)
        return self._unwrap(r)

    # ----- helpers -----

    @staticmethod
    def _unwrap(r: httpx.Response) -> dict[str, Any]:
        if 200 <= r.status_code < 300:
            return r.json() if r.content else {}
        try:
            body: dict[str, Any] = r.json()
        except Exception:
            body = {"raw": r.text}
        raise RegistryError(r.status_code, body)
