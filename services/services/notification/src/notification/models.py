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


class NotifyRequest(BaseModel):
    template_code: str
    channel_type: str
    recipient: str = ""
    variables: dict = Field(default_factory=dict)
    locale: str = "zh-CN"


class NotifyResult(BaseModel):
    success: bool
    error: str | None = None
    provider_msg_id: str | None = None


class ChannelConfigCreate(BaseModel):
    channel_type: str
    name: str = "default"
    config: dict
    status: str = "active"


class ChannelConfigUpdate(BaseModel):
    channel_type: str | None = None
    name: str | None = None
    config: dict | None = None
    status: str | None = None


class ChannelConfigResponse(BaseModel):
    id: str
    channel_type: str
    name: str
    config: dict
    status: str
