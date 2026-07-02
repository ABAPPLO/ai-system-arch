"""共享 fixtures（dispatcher tests）。"""

import pytest

from apihub_core.tenant import clear_tenant_context


@pytest.fixture(autouse=True)
def reset_tenant_context():
    clear_tenant_context()
    yield
    clear_tenant_context()


@pytest.fixture
def tenant_a():
    from apihub_core.tenant import TenantContext
    return TenantContext(
        tenant_id="tenant_a",
        tenant_type="internal",
        app_id="app_trading",
    )
