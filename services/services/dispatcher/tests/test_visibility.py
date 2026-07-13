"""dispatcher visibility 三级授权单测（纯函数，不依赖 DB）。"""

import pytest
from apihub_core.errors import ApiError
from apihub_core.tenant import TenantContext
from dispatcher.visibility import check_visibility


def _snap(visibility, tenant_id="tenant_a"):
    return type("S", (), {"visibility": visibility, "tenant_id": tenant_id})()


def test_public_allows_any_tenant():
    ctx = TenantContext(tenant_id="external-public", tenant_type="external")
    check_visibility(_snap("public", "tenant_a"), ctx)  # 不 raise


def test_tenant_blocks_other_tenant():
    ctx = TenantContext(tenant_id="external-public", tenant_type="external")
    with pytest.raises(ApiError) as exc:
        check_visibility(_snap("tenant", "tenant_a"), ctx)
    assert exc.value.http_status == 403


def test_tenant_allows_same_tenant():
    ctx = TenantContext(tenant_id="tenant_a", tenant_type="internal")
    check_visibility(_snap("tenant", "tenant_a"), ctx)


def test_private_requires_platform_admin():
    ctx = TenantContext(tenant_id="tenant_a", tenant_type="internal", is_platform_admin=False)
    with pytest.raises(ApiError):
        check_visibility(_snap("private", "tenant_a"), ctx)
    from dataclasses import replace
    check_visibility(_snap("private", "tenant_a"), replace(ctx, is_platform_admin=True))
