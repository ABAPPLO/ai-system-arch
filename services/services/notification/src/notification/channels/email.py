"""邮件渠道（aiosmtplib）。tenant 配置缺失时回退平台 env。"""

from __future__ import annotations

import os
from email.message import EmailMessage

import aiosmtplib

from notification.channels.base import Channel, NotificationMessage, SendResult


def _platform_smtp() -> dict:
    """平台默认 SMTP（env 未设则空 dict）。"""
    host = os.environ.get("NOTIFICATION_SMTP_HOST")
    if not host:
        return {}
    return {
        "smtp_host": host,
        "smtp_port": os.environ.get("NOTIFICATION_SMTP_PORT", "587"),
        "smtp_user": os.environ.get("NOTIFICATION_SMTP_USER", ""),
        "smtp_password": os.environ.get("NOTIFICATION_SMTP_PASSWORD", ""),
        "from_addr": os.environ.get("NOTIFICATION_SMTP_FROM_ADDR", ""),
        "use_tls": os.environ.get("NOTIFICATION_SMTP_USE_TLS", "false").lower()
        in ("1", "true", "yes"),
    }


class EmailChannel(Channel):
    channel_type = "email"

    async def send(self, message: NotificationMessage) -> SendResult:
        cfg = {**_platform_smtp(), **(message.config or {})}
        host = cfg.get("smtp_host")
        if not host:
            return SendResult(success=False, error="no smtp config")
        from_addr = cfg.get("from_addr") or message.recipient
        msg = EmailMessage()
        msg["From"] = from_addr
        msg["To"] = message.recipient
        msg["Subject"] = message.subject
        msg.set_content(message.body)
        try:
            async with aiosmtplib.SMTP(
                host, int(cfg.get("smtp_port", 587)),
                use_tls=bool(cfg.get("use_tls")), timeout=10,
            ) as smtp:
                user, pwd = cfg.get("smtp_user"), cfg.get("smtp_password")
                if user:
                    await smtp.login(user, pwd or "")
                await smtp.send_message(msg)
        except Exception as e:  # 网络/Auth 失败→业务结果，不抛
            return SendResult(success=False, error=str(e))
        return SendResult(success=True, provider_msg_id=None)
