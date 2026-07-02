"""共享 fixtures（executor tests）。"""

import os

# 必须在 import apihub_core 之前注入最小 env，避免 Settings 校验炸。
_ENV_DEFAULTS = {
    "PG_HOST": "localhost",
    "PG_USER": "apihub",
    "PG_PASSWORD": "test",
    "REDIS_HOST": "localhost",
    "REDIS_PORT": "6379",
    "ENV": "test",
    "OTEL_SERVICE_NAME": "executor-test",
}
for k, v in _ENV_DEFAULTS.items():
    os.environ.setdefault(k, v)

import pytest  # noqa: E402
from apihub_core.config import get_settings  # noqa: E402


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
