"""quota 启动入口 —— 含 T+1 计费聚合。"""

import asyncio
from datetime import datetime, timedelta

from apihub_core import create_app, db
from apihub_core.clickhouse import query_all

from quota.routes import register_routes


async def _daily_billing_aggregation(settings):
    """每日 02:00 将前一日 api_call_log 聚合写入 billing_record。"""
    while True:
        now = datetime.utcnow()
        target = now.replace(hour=2, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())

        yesterday = target - timedelta(days=1)
        month_start = yesterday.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        month_end = (month_start + timedelta(days=32)).replace(day=1) - timedelta(seconds=1)

        rows = query_all(
            "SELECT tenant_id, count() AS call_count, sum(token_count) AS token_count"
            " FROM api_call_log"
            " WHERE ts >= %(start)s AND ts < %(end)s"
            " GROUP BY tenant_id",
            params={
                "start": yesterday.replace(hour=0, minute=0, second=0),
                "end": yesterday.replace(hour=23, minute=59, second=59),
            },
            force_tenant_id=None,
        )
        async with db.admin_db_session() as conn:
            for row in rows:
                sub = await conn.fetchrow(
                    "SELECT id FROM subscription WHERE tenant_id = $1 AND status = 'active' LIMIT 1",
                    row["tenant_id"],
                )
                if not sub:
                    continue
                exists = await conn.fetchval(
                    "SELECT 1 FROM billing_record WHERE tenant_id = $1 AND period_start = $2",
                    row["tenant_id"],
                    month_start,
                )
                if exists:
                    continue
                await conn.execute(
                    "INSERT INTO billing_record"
                    " (tenant_id, subscription_id, period_start, period_end,"
                    "  call_count, token_count, base_charge_cents, overage_charge_cents,"
                    "  total_charge_cents, status)"
                    " VALUES ($1,$2,$3,$4,$5,$6,0,0,0,'pending')",
                    row["tenant_id"],
                    sub["id"],
                    month_start,
                    month_end,
                    row["call_count"],
                    row["token_count"],
                )


def build_app():
    return create_app(
        service_name="quota",
        build_routes=register_routes,
        skip_auth_paths=(
            "/health",
            "/metrics",
            "/v1/quota/health",
            "/v1/quota/check",
            "/v1/quota/check-strict",
            "/v1/quota/refund",
            "/docs",
            "/openapi.json",
        ),
        extra_lifespan=_daily_billing_aggregation,
    )


app = build_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("quota.main:app", host="0.0.0.0", port=8004, workers=4, log_level="info")
