"""Channel 抽象基类与消息/结果数据类。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class NotificationMessage:
    recipient: str
    subject: str
    body: str
    channel_type: str
    config: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)


@dataclass
class SendResult:
    success: bool
    error: str | None = None
    provider_msg_id: str | None = None


class Channel(ABC):
    channel_type: str = ""

    @abstractmethod
    async def send(self, message: NotificationMessage) -> SendResult:
        """发送。永不抛异常——失败返回 SendResult(success=False, error=...)。"""
