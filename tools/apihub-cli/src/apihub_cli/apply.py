"""Apply 编排：YAML → api-registry（API / Version / ChangeRequest）。

dev 环境：submit → 自动 applied（ADR-005 自服务）。
staging / prod：仅 submit，等审批。
"""

from dataclasses import dataclass
from typing import Any

from apihub_cli.client import RegistryClient, RegistryError
from apihub_cli.models import ApiDefinition, BackendType


@dataclass
class ApplyResult:
    """单个 YAML 的 apply 结果。"""

    source: str
    api_name: str
    api_id: str | None
    version_id: str | None
    change_request_id: int | None
    change_request_status: str | None  # approved / pending
    error: str | None = None

    def is_ok(self) -> bool:
        return self.error is None


def apply_definition(
    defn: ApiDefinition,
    *,
    client: RegistryClient,
    target_env: str,
    submitted_by: str,
) -> ApplyResult:
    """把一份 ApiDefinition 推到 api-registry。

    步骤：
      1. 找/建 api 记录（按 name 去重）
      2. 建 version（每次 apply 都新增一行 —— Phase 2 简化，未来加 diff 跳过）
      3. submit change_request（dev 自动 apply，staging/prod pending）
    """
    src = defn.source_file or "<unknown>"
    try:
        # 1) api
        existing = client.find_api_by_name(defn.api.name)
        if existing is None:
            api_id = client.create_api(
                {
                    "name": defn.api.name,
                    "description": defn.api.description,
                    "category": defn.api.category,
                    "base_path": defn.api.base_path,
                    "tags": defn.api.tags,
                }
            )
        else:
            api_id = existing["id"]

        # 2) version
        version_payload: dict[str, Any] = {
            "api_id": api_id,
            "version": defn.version.version,
            "backend_type": defn.version.backend_type.value,
            "backend_url": defn.version.backend_url,
            "method": defn.version.method.value,
            "path": defn.version.path,
        }
        if defn.version.request_schema is not None:
            version_payload["request_schema"] = defn.version.request_schema
        if defn.version.response_schema is not None:
            version_payload["response_schema"] = defn.version.response_schema
        if defn.version.masking is not None:
            version_payload["masking"] = defn.version.masking
        if defn.version.backend_type == BackendType.AI_MODEL:
            version_payload["ai_model"] = defn.version.ai_model
            version_payload["ai_streaming"] = defn.version.ai_streaming

        version_resp = client.create_version(version_payload)
        version_id = version_resp.get("id")

        # 3) change_request
        cr_payload = {
            "api_id": api_id,
            "target_version": defn.version.version,
            "change_type": "create" if existing is None else "update",
            "target_env": target_env,
            "proposed_config": defn.proposed_config(),
            "submitted_by": submitted_by,
        }
        cr_resp = client.submit_change_request(cr_payload)

        return ApplyResult(
            source=src,
            api_name=defn.api.name,
            api_id=api_id,
            version_id=version_id,
            change_request_id=cr_resp.get("request_id"),
            change_request_status=cr_resp.get("status"),
        )
    except RegistryError as e:
        return ApplyResult(
            source=src,
            api_name=defn.api.name,
            api_id=None,
            version_id=None,
            change_request_id=None,
            change_request_status=None,
            error=f"HTTP {e.status}: {e.payload}",
        )
    except Exception as e:  # noqa: BLE001
        return ApplyResult(
            source=src,
            api_name=defn.api.name,
            api_id=None,
            version_id=None,
            change_request_id=None,
            change_request_status=None,
            error=f"{type(e).__name__}: {e}",
        )
