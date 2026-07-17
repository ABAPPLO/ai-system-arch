"""钉钉自定义机器人渠道（HMAC-SHA256 加签）。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
import urllib.parse

import httpx

from notification.channels.base import Channel, NotificationMessage, SendResult


def _sign(timestamp_ms: int, secret: str) -> str:
    string_to_sign = f"{timestamp_ms}\n{secret}"
    digest = hmac.new(
        secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256
    ).digest()
    return urllib.parse.quote_plus(base64.b64encode(digest))


class DingTalkChannel(Channel):
    channel_type = "dingtalk"

    async def send(self, message: NotificationMessage) -> SendResult:
        cfg = message.config or {}
        webhook_url = cfg.get("webhook_url")
        if not webhook_url:
            return SendResult(success=False, error="no webhook_url")
        secret = cfg.get("secret") or ""
        url = webhook_url
        if secret:
            ts = int(time.time() * 1000)
            url = f"{webhook_url}&timestamp={ts}&sign={_sign(ts, secret)}"
        payload = {
            "msgtype": "markdown",
            "markdown": {"title": message.subject or "notification", "text": message.body},
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                resp = await c.post(url, json=payload)
            data = resp.json()
        except Exception as e:
            return SendResult(success=False, error=str(e))
        if data.get("errcode") != 0:
            return SendResult(success=False, error=str(data.get("errmsg", "dingtalk error")))
        return SendResult(success=True, provider_msg_id=data.get("msgid") or data.get("taskId"))
