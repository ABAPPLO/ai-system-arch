"""notification Pydantic 模型。"""

from pydantic import BaseModel, Field


class WebhookCreate(BaseModel):
    url: str = Field(min_length=5, max_length=512)
    events: list[str] = Field(min_length=1)
    secret: str | None = None
    status: str = "active"


class WebhookUpdate(BaseModel):
    url: str | None = None
    events: list[str] | None = None
    secret: str | None = None
    status: str | None = None


class WebhookResponse(BaseModel):
    id: str
    url: str
    events: list[str]
    status: str
    created_at: str


class WebhookTestResult(BaseModel):
    success: bool
    status_code: int | None = None
    latency_ms: int | None = None
    error: str | None = None
