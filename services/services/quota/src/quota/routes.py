"""quota 路由。

端点（网关内部 + 管理后台）：
  POST /v1/quota/check    —— 网关 dispatcher 调，check + 原子扣减
  POST /v1/quota/refund   —— 业务失败时退回（best-effort）
  GET  /v1/quota/usage    —— 管理后台查用量
  GET  /v1/quota/billing  —— Phase 3 占位
  GET  /v1/quota/health   —— 自身健康
"""

from apihub_core import kafka
from apihub_core.errors import ApiError, ErrorCode
from apihub_core.logging import get_logger
from apihub_core.tenant import require_tenant
from fastapi import FastAPI

from quota import repository
from quota.limiter import (
    check_and_consume,
    get_usage,
    raise_if_blocked,
)
from quota.limiter import (
    refund as refund_quota,
)
from quota.models import (
    BillingResponse,
    DailyApiUsage,
    PlanSummary,
    QuotaCheckRequest,
    QuotaCheckResponse,
    QuotaRefundRequest,
    UsageResponse,
)

log = get_logger(__name__)


def register_routes(app: FastAPI) -> None:
    @app.post("/v1/quota/check", response_model=QuotaCheckResponse)
    async def check(payload: QuotaCheckRequest):
        """网关调用：原子 check + 扣减。

        失败响应也是 200（业务层语义），allowed=False 表示被挡。
        429 只在 raise_if_blocked 抛 ApiError 时出现（部分场景调用方期望直接抛错）。
        """
        rules, source = await repository.load_rules(
            payload.tenant_id, payload.app_id, payload.api_id
        )

        resp = await check_and_consume(
            payload.tenant_id,
            payload.app_id,
            payload.api_id,
            rules,
            cost=payload.cost,
        )
        resp.rule_source = resp.rule_source if resp.rule_source == "fallback" else source

        # 推一条限流决策事件给 ClickHouse（计费 + 趋势分析）
        try:
            await kafka.emit(
                "api-call-events",
                {
                    "tenant_id": payload.tenant_id,
                    "app_id": payload.app_id,
                    "api_id": payload.api_id,
                    "event_type": "quota_check",
                    "allowed": resp.allowed,
                    "tier_blocked": resp.tier_blocked,
                    "cost": payload.cost,
                    "rule_source": resp.rule_source,
                },
                key=f"{payload.tenant_id}:{payload.app_id}:{payload.api_id}",
            )
        except Exception as e:
            log.warning("quota_emit_failed", error=str(e))

        return resp

    @app.post("/v1/quota/check-strict", response_model=QuotaCheckResponse)
    async def check_strict(payload: QuotaCheckRequest):
        """严格版：超了直接抛 429。给不想自己处理 allowed=False 的调用方用。"""
        rules, source = await repository.load_rules(
            payload.tenant_id, payload.app_id, payload.api_id
        )
        resp = await check_and_consume(
            payload.tenant_id,
            payload.app_id,
            payload.api_id,
            rules,
            cost=payload.cost,
        )
        resp.rule_source = source
        raise_if_blocked(resp)  # 超了抛 ApiError(429)
        return resp

    @app.post("/v1/quota/refund")
    async def refund_endpoint(payload: QuotaRefundRequest):
        """退回扣减（业务失败时）。best-effort，失败也返回 200。"""
        ok = await refund_quota(
            payload.tenant_id,
            payload.app_id,
            payload.api_id,
            cost=payload.cost,
        )
        return {"refunded": ok}

    @app.get("/v1/quota/usage", response_model=UsageResponse)
    async def usage(tenant_id: str, app_id: str, api_id: str):
        """查当前用量（不扣减）。"""
        ctx = require_tenant()
        # 租户隔离：只能查自己的（超管除外）
        if ctx.tenant_id != tenant_id and not ctx.is_platform_admin:
            raise ApiError(ErrorCode.FORBIDDEN, "cannot view other tenant's usage")

        rules, _ = await repository.load_rules(tenant_id, app_id, api_id)
        return await get_usage(tenant_id, app_id, api_id, rules)

    @app.get("/v1/quota/billing", response_model=BillingResponse)
    async def billing(tenant_id: str, month: str):
        """按月查询用量+plan——Phase 3 真实现。"""
        ctx = require_tenant()
        if ctx.tenant_id != tenant_id and not ctx.is_platform_admin:
            raise ApiError(ErrorCode.FORBIDDEN, "cannot view other tenant's billing")

        sub = await repository.get_active_subscription(tenant_id)
        plan = await repository.get_plan(sub["plan_code"]) if sub else None
        ch_rows = await repository.get_billing_from_ch(tenant_id, month)
        remaining = await repository.get_remaining_calls_today(tenant_id)

        daily_usage = [
            DailyApiUsage(
                api_id=r["api_id"],
                day=str(r["day"]),
                calls=r["calls"],
                tokens=r["tokens"],
                latency_ms=r["latency_ms"],
            )
            for r in ch_rows
        ]
        return BillingResponse(
            tenant_id=tenant_id,
            month=month,
            plan=plan
            or PlanSummary(code="free", name="Free", price_cents=0, quota_included={}, features={}),
            daily_usage=daily_usage,
            total_calls=sum(u.calls for u in daily_usage),
            total_tokens=sum(u.tokens for u in daily_usage),
            remaining_calls_today=remaining,
        )

    @app.get("/v1/quota/plans")
    async def plans():
        """Plan 列表（对比用）。"""
        return await repository.list_plans()

    @app.get("/v1/quota/health")
    async def health():
        return {"status": "ok", "service": "quota"}
