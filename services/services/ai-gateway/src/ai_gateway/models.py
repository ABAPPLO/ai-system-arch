"""AI 网关 Pydantic 模型。"""

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel


class Message(BaseModel):
    role: str = "user"
    content: str = ""


class ChatRequest(BaseModel):
    model: str
    messages: list[Message] = []
    stream: bool | None = True
    temperature: float | None = None
    max_tokens: int | None = None
    extra_body: dict[str, Any] | None = None


class ChatResponseChoice(BaseModel):
    index: int = 0
    message: Message | None = None
    finish_reason: str | None = None


class ChatResponse(BaseModel):
    id: str = ""
    object: str = "chat.completion"
    choices: list[ChatResponseChoice] = []
    usage: dict[str, int] = {}


@dataclass
class SSEChunk:
    content: str = ""
    finish_reason: str | None = None
    usage: dict | None = None


@dataclass
class RouteResult:
    target_provider_id: str
    target_model: str
    provider_type: str
    base_url: str
    provider_key_encrypted: str
