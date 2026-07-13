"""AI 网关测试共享 fixtures。"""

import os
import sys

# 开发机有 all_proxy=socks://127.0.0.1:12347/ 等代理环境变量，
# 但 httpx 不认 socks:// scheme（只用 socks5:// / socks5h://），
# 导致 httpx.AsyncClient.__init__ 报 ValueError。
# 在 import httpx 之前删掉所有代理 env var。
for _k in ("all_proxy", "ALL_PROXY", "http_proxy", "HTTP_PROXY",
           "https_proxy", "HTTPS_PROXY", "no_proxy", "NO_PROXY"):
    os.environ.pop(_k, None)

# 在 import apihub_core 之前注入最小环境变量（避免 Settings() 因缺 PG_HOST 等报错）。
os.environ.setdefault("PG_HOST", "localhost")
os.environ.setdefault("PG_USER", "apihub")
os.environ.setdefault("PG_PASSWORD", "test")
os.environ.setdefault("REDIS_HOST", "localhost")
os.environ.setdefault("REDIS_PORT", "6379")
os.environ.setdefault("ENV", "test")
os.environ.setdefault("OTEL_SERVICE_NAME", "ai-gateway-test")

# 必须在 ai_gateway.main（模块级 app = create_app(...)）之前设好 AI_GATEWAY_ENCRYPTION_KEY
os.environ.setdefault("AI_GATEWAY_ENCRYPTION_KEY", "a" * 64)

import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from apihub_core.config import get_settings  # noqa: E402

# 清除模块级缓存 —— 让第一次调用 get_settings() 拿到我们的 ENV_DEFAULTS
get_settings.cache_clear()


@pytest.fixture(autouse=True)
def clear_settings_cache():
    """每个测试都重新构造 Settings，避免前一个测试的 monkeypatch 残留。"""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client():
    get_settings.cache_clear()
    from ai_gateway.main import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
