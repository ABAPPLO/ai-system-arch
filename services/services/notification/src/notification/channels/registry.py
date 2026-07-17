"""渠道注册表。"""

from __future__ import annotations

from notification.channels.base import Channel
from notification.channels.dingtalk import DingTalkChannel
from notification.channels.email import EmailChannel

_REGISTRY: dict[str, Channel] = {
    "email": EmailChannel(),
    "dingtalk": DingTalkChannel(),
}


def get(channel_type: str) -> Channel:
    ch = _REGISTRY.get(channel_type)
    if ch is None:
        from apihub_core.errors import ApiError, ErrorCode
        raise ApiError(ErrorCode.INVALID_INPUT, f"unsupported channel_type: {channel_type}")
    return ch


def _register(channel: Channel) -> None:
    _REGISTRY[channel.channel_type] = channel
