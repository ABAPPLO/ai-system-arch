"""Webhook URL 的 SSRF 防护。

用户可自助登记 webhook URL，平台后台会持续向其投递数据 —— 等于把
「以平台身份发 HTTP 请求」的能力交了出去。这里做三道收口：

1. 创建/更新时：scheme 限 http(s)、主机名禁内网后缀、DNS 解析后全部
   IP 必须是公网地址（拦 127.0.0.1 / 10.x / 172.16-31.x / 192.168.x /
   169.254.169.254 云 metadata / IPv6 ULA 等）。
2. 投递时（consumer/_test_webhook）：再次解析校验，拦「创建时公网、
   投递时 DNS 重绑到内网」的常见绕过。
3. 残余风险（DNS rebinding TOCTOU）需出向防火墙 / egress proxy 兜底，
   本模块只做应用层能做的部分。
"""

import ipaddress
import socket
from urllib.parse import urlparse

from apihub_core.errors import ApiError, ErrorCode

# 集群/内网常见主机后缀（DNS 解析之外的廉价预检）
_FORBIDDEN_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".svc",
    ".svc.cluster.local",
    ".cluster.local",
)


def _assert_ip_public(ip_str: str) -> None:
    addr = ipaddress.ip_address(ip_str)
    if (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    ):
        raise ApiError(
            ErrorCode.INVALID_INPUT,
            f"webhook host resolves to non-public IP: {ip_str}",
            http_status=400,
        )


def _assert_host_public(host: str) -> None:
    """host 为 IP 字面量时直接校验；否则 DNS 解析，全部 IP 必须是公网。"""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        _assert_ip_public(host)
        return

    lowered = host.lower()
    if lowered == "localhost" or any(lowered.endswith(suf) for suf in _FORBIDDEN_HOST_SUFFIXES):
        raise ApiError(
            ErrorCode.INVALID_INPUT, f"webhook host not allowed: {host}", http_status=400
        )

    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as e:
        raise ApiError(
            ErrorCode.INVALID_INPUT, f"webhook host unresolvable: {host}", http_status=400
        ) from e
    for info in infos:
        ip = info[4][0]
        if isinstance(ip, str):  # AF_INET6 的 sockaddr 首位才是 host str，其余跳过
            _assert_ip_public(ip)


def assert_public_webhook_url(url: str) -> None:
    """校验 webhook URL（创建/更新/投递前调用），非法即抛 400。

    含阻塞 DNS 解析（socket.getaddrinfo），异步调用方须 asyncio.to_thread 包装。
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ApiError(
            ErrorCode.INVALID_INPUT, "webhook URL scheme must be http/https", http_status=400
        )
    host = parsed.hostname
    if not host:
        raise ApiError(ErrorCode.INVALID_INPUT, "webhook URL has no host", http_status=400)
    _assert_host_public(host)
