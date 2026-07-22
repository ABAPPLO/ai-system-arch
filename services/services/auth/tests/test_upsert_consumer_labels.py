import asyncio
from unittest.mock import AsyncMock, patch

from apihub_core.apisix_client import upsert_consumer


def test_upsert_consumer_includes_home_region_label():
    captured = {}

    async def fake(method, url, **kw):
        captured["body"] = kw.get("json")

        class R:
            status_code = 201

        return R()

    with (
        patch("apihub_core.apisix_client._admin_request", new=AsyncMock(side_effect=fake)),
        patch("apihub_core.apisix_client.get_settings") as gs,
    ):
        gs.return_value.apisix_admin_url = "http://x"
        asyncio.run(upsert_consumer(key_id="k1", key="sekret", labels={"home_region": "bj"}))
    assert captured["body"]["labels"]["home_region"] == "bj"
    assert captured["body"]["plugins"]["key-auth"]["key"] == "sekret"


def test_upsert_consumer_no_labels_omits_field():
    captured = {}

    async def fake(method, url, **kw):
        captured["body"] = kw.get("json")

        class R:
            status_code = 201

        return R()

    with (
        patch("apihub_core.apisix_client._admin_request", new=AsyncMock(side_effect=fake)),
        patch("apihub_core.apisix_client.get_settings") as gs,
    ):
        gs.return_value.apisix_admin_url = "http://x"
        asyncio.run(upsert_consumer(key_id="k1", key="sekret"))
    assert "labels" not in captured["body"]
