"""共享 fixtures。"""

import pytest
from apihub_core.tenant import clear_tenant_context


@pytest.fixture(autouse=True)
def reset_tenant_context():
    """每个测试前后清掉 tenant context，避免相互污染。"""
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


@pytest.fixture
def tenant_b():
    from apihub_core.tenant import TenantContext

    return TenantContext(
        tenant_id="tenant_b",
        tenant_type="internal",
        app_id="app_risk",
    )


@pytest.fixture
def tenant_admin():
    from apihub_core.tenant import TenantContext

    return TenantContext(
        tenant_id="",
        tenant_type="system",
        is_platform_admin=True,
    )
