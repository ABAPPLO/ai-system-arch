"""请求 / 响应 / 规则模型。"""

from pydantic import BaseModel, Field

# 三 tier 的固定窗口长度（秒）
TIER_SECOND = 1
TIER_MINUTE = 60
TIER_DAY = 86400

# tier 名 → 窗口长度（顺序：秒、分、日）
TIER_WINDOWS = (TIER_SECOND, TIER_MINUTE, TIER_DAY)


class LimitRule(BaseModel):
    """单 tier 限流规则。"""

    window_seconds: int = Field(gt=0)
    max_count: int = Field(ge=0)
    enabled: bool = True


class QuotaRules(BaseModel):
    """合并后的三层规则 —— app > tenant > api_version > default。

    每层都可独立配置；未配则用上层；全未配则 disabled（不限流）。
    """

    second: LimitRule | None = None
    minute: LimitRule | None = None
    day: LimitRule | None = None


class QuotaCheckRequest(BaseModel):
    """网关调 dispatcher 前调本服务，问能不能放行。"""

    tenant_id: str = Field(min_length=1)
    app_id: str = Field(min_length=1)
    api_id: str = Field(min_length=1)
    cost: int = Field(default=1, ge=1, le=1000)  # 一次调用扣几个（AI 按 token 折算）


class QuotaCheckResponse(BaseModel):
    """决策结果。

    allowed=False 时客户端应直接返回 429；retry_after_seconds 给出
    最近一层窗口剩余时间，调用方可以带回 Retry-After header。
    """

    allowed: bool
    tier_blocked: str | None = None  # "second" / "minute" / "day" / None
    limit: int | None = None  # 哪一层超了，对应的 max
    remaining: int | None = None  # 还剩多少（仅 allowed=True 有意义）
    retry_after_seconds: int = 0
    rule_source: str = "default"  # app / tenant / api_version / default


class QuotaRefundRequest(BaseModel):
    """调用失败时退回扣的配额（best-effort，不阻塞业务）。"""

    tenant_id: str
    app_id: str
    api_id: str
    cost: int = Field(default=1, ge=1, le=1000)


class UsagePoint(BaseModel):
    """单 tier 当前用量。"""

    window_seconds: int
    used: int
    limit: int | None = None  # None = 不限流


class UsageResponse(BaseModel):
    tenant_id: str
    app_id: str
    api_id: str
    second: UsagePoint
    minute: UsagePoint
    day: UsagePoint
