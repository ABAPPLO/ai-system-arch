"""docs-svc 数据访问 —— 从 PG 读 api + api_version 元数据。

通过 RLS 自动限制租户可见范围（docs 是只读消费方）。
"""

from typing import Any

from apihub_core import db
from apihub_core.errors import ApiError, ErrorCode

from docs.models import ApiMeta


async def get_api_meta(api_id: str, version: str | None = None) -> ApiMeta:
    """取 api + 最新（或指定）版本的元数据。

    返回 ApiMeta；找不到抛 NOT_FOUND。
    """
    async with db.db_session() as conn:
        api_row = await conn.fetchrow(
            """
            SELECT id, name, description, category, base_path, tags, status
            FROM api
            WHERE id = $1
            """,
            api_id,
        )
        if not api_row:
            raise ApiError(ErrorCode.NOT_FOUND, f"API {api_id} not found")

        if version is None:
            ver_row = await conn.fetchrow(
                """
                SELECT id, version, backend_type, backend_url, status,
                       request_schema, response_schema, masking,
                       ai_model, ai_streaming
                FROM api_version
                WHERE api_id = $1 AND status IN ('published', 'deprecated')
                ORDER BY
                    CASE status WHEN 'published' THEN 0 ELSE 1 END,
                    created_at DESC
                LIMIT 1
                """,
                api_id,
            )
        else:
            ver_row = await conn.fetchrow(
                """
                SELECT id, version, backend_type, backend_url, status,
                       request_schema, response_schema, masking,
                       ai_model, ai_streaming
                FROM api_version
                WHERE api_id = $1 AND version = $2
                LIMIT 1
                """,
                api_id,
                version,
            )

        if not ver_row:
            raise ApiError(
                ErrorCode.NOT_FOUND,
                f"No version found for api {api_id}"
                + (f" version={version}" if version else " (no published version)"),
            )

    return _row_to_meta(api_row, ver_row)


async def list_versions(api_id: str) -> list[dict[str, Any]]:
    """列某 api 的所有版本（docs 页面切换用）。"""
    async with db.db_session() as conn:
        rows = await conn.fetch(
            """
            SELECT id, version, status, backend_type, created_at, published_at
            FROM api_version
            WHERE api_id = $1
            ORDER BY created_at DESC
            """,
            api_id,
        )
    return [dict(r) for r in rows]


def _row_to_meta(api_row: Any, ver_row: Any) -> ApiMeta:
    return ApiMeta(
        api_id=api_row["id"],
        api_name=api_row["name"],
        description=api_row["description"],
        category=api_row["category"],
        base_path=api_row["base_path"],
        tags=api_row["tags"] or [],
        api_status=api_row["status"],
        version_id=ver_row["id"],
        version=ver_row["version"],
        backend_type=ver_row["backend_type"],
        backend_url=ver_row["backend_url"],
        version_status=ver_row["status"],
        request_schema=ver_row["request_schema"],
        response_schema=ver_row["response_schema"],
        masking=ver_row["masking"],
        ai_model=ver_row["ai_model"],
        ai_streaming=ver_row["ai_streaming"],
    )
