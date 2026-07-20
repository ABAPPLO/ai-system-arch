"""S1-T3: create_key 注入 home_region label（多区写亲和 consumer 侧）。

验 `_inject_home_region_on_create` 这个 testable seam：
  - tenant 有 home_region → labels={"home_region": <hr>}
  - tenant 无 home_region → labels=None
"""

import asyncio
from unittest.mock import AsyncMock, patch


def test_create_key_passes_home_region_label():
    """create_key path looks up tenant.home_region and passes it as a label."""
    from auth import routes

    with patch("auth.routes.upsert_consumer", new=AsyncMock()) as up, \
         patch("auth.routes.get_tenant_home_region", new=AsyncMock(return_value="bj")):
        asyncio.run(
            routes._inject_home_region_on_create(
                key_id="k1", key="sekret", tenant_id="t_bj"
            )
        )
        up.assert_awaited_once_with(
            key_id="k1", key="sekret", labels={"home_region": "bj"}
        )


def test_create_key_no_home_region_omits_labels():
    from auth import routes

    with patch("auth.routes.upsert_consumer", new=AsyncMock()) as up, \
         patch("auth.routes.get_tenant_home_region", new=AsyncMock(return_value=None)):
        asyncio.run(
            routes._inject_home_region_on_create(
                key_id="k1", key="sekret", tenant_id="t_none"
            )
        )
        up.assert_awaited_once_with(key_id="k1", key="sekret", labels=None)
