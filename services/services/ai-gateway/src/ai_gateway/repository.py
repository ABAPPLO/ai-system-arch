"""AI 网关数据访问 —— 模型路由查询 + Provider Key 解密。"""

from apihub_core import db

from ai_gateway.models import RouteResult


async def resolve_model_route(model: str) -> RouteResult | None:
    async with db.admin_db_session() as conn:
        row = await conn.fetchrow(
            """
            SELECT mr.target_provider_id, mr.target_model,
                   p.provider_type, p.base_url, pk.key_encrypted
            FROM ai_model_route mr
            JOIN ai_provider p ON p.id = mr.target_provider_id
            JOIN ai_provider_key pk ON pk.provider_id = p.id AND pk.status = 'active'
            WHERE mr.status = 'active'
              AND ($1 ILIKE mr.model_pattern)
            ORDER BY mr.priority DESC, LENGTH(mr.model_pattern) DESC
            LIMIT 1
            """,
            model,
        )
        if not row:
            return None

        return RouteResult(
            target_provider_id=str(row["target_provider_id"]),
            target_model=row["target_model"],
            provider_type=row["provider_type"],
            base_url=row["base_url"],
            provider_key_encrypted=row["key_encrypted"],
        )
