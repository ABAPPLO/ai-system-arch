"""ssrf 校验测试 —— webhook URL 的 scheme / 主机 / IP 三道检查。

IP 字面量与后缀检查不依赖网络；DNS 用例 monkeypatch socket.getaddrinfo。
"""

import pytest
from apihub_core.errors import ApiError
from notification.ssrf import assert_public_webhook_url


class TestScheme:
    @pytest.mark.parametrize(
        "url",
        ["ftp://example.com/hook", "file:///etc/passwd", "gopher://x", "javascript:alert(1)"],
    )
    def test_non_http_rejected(self, url):
        with pytest.raises(ApiError):
            assert_public_webhook_url(url)

    def test_no_host_rejected(self):
        with pytest.raises(ApiError):
            assert_public_webhook_url("http://")


class TestIPLiteral:
    @pytest.mark.parametrize(
        "host",
        [
            "127.0.0.1",
            "10.0.0.1",
            "192.168.1.1",
            "172.16.0.1",
            "172.31.255.255",
            "169.254.169.254",  # 云 metadata
            "0.0.0.0",
            "224.0.0.1",
            "240.0.0.1",
            "::1",
            "fe80::1",  # IPv6 link-local
            "fd00::1",  # IPv6 ULA
        ],
    )
    def test_private_or_reserved_rejected(self, host):
        with pytest.raises(ApiError):
            assert_public_webhook_url(f"https://{host}/hook")

    def test_public_ip_ok(self):
        assert_public_webhook_url("https://8.8.8.8/hook")


class TestHostnameSuffix:
    @pytest.mark.parametrize(
        "host",
        [
            "localhost",
            "db.internal",
            "apihub-rds.internal",
            "auth.apihub-system.svc",
            "x.default.svc.cluster.local",
            "cluster.local",
        ],
    )
    def test_internal_suffix_rejected(self, host):
        with pytest.raises(ApiError):
            assert_public_webhook_url(f"https://{host}/hook")


class TestDNSResolution:
    """monkeypatch getaddrinfo —— 不打真实网络。"""

    def test_resolves_to_private_rejected(self, monkeypatch):
        import socket

        def _fake(host, port, *a, **kw):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 0))]

        monkeypatch.setattr(socket, "getaddrinfo", _fake)
        with pytest.raises(ApiError):
            assert_public_webhook_url("https://rebind.example/hook")

    def test_multi_record_any_private_rejected(self, monkeypatch):
        """A 记录一张公网一张内网 —— 有一张内网就拒。"""
        import socket

        def _fake(host, port, *a, **kw):
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.0.5", 0)),
            ]

        monkeypatch.setattr(socket, "getaddrinfo", _fake)
        with pytest.raises(ApiError):
            assert_public_webhook_url("https://mixed.example/hook")

    def test_resolves_to_public_ok(self, monkeypatch):
        import socket

        def _fake(host, port, *a, **kw):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

        monkeypatch.setattr(socket, "getaddrinfo", _fake)
        assert_public_webhook_url("https://ok.example/hook")

    def test_unresolvable_rejected(self, monkeypatch):
        import socket

        def _fake(host, port, *a, **kw):
            raise OSError("NXDOMAIN")

        monkeypatch.setattr(socket, "getaddrinfo", _fake)
        with pytest.raises(ApiError):
            assert_public_webhook_url("https://nonexistent.invalid/hook")
