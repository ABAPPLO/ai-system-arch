"""notification 渠道层。"""

from notification.channels.base import Channel, NotificationMessage, SendResult
from notification.channels.registry import get

__all__ = ["Channel", "NotificationMessage", "SendResult", "get"]
