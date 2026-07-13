import os
for k in ("all_proxy","ALL_PROXY","http_proxy","HTTP_PROXY","https_proxy","HTTPS_PROXY","no_proxy","NO_PROXY"):
    os.environ.pop(k, None)
os.environ.setdefault("PG_HOST","localhost"); os.environ.setdefault("PG_USER","apihub")
os.environ.setdefault("PG_PASSWORD","test"); os.environ.setdefault("REDIS_HOST","localhost")
os.environ.setdefault("ENV","test")
import pytest
from apihub_core.config import get_settings; get_settings.cache_clear()
from httpx import ASGITransport, AsyncClient

@pytest.fixture(autouse=True)
def clear_cache():
    get_settings.cache_clear(); yield; get_settings.cache_clear()

@pytest.fixture
def client():
    from billing.main import app
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
