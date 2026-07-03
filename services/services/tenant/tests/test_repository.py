"""repository 单测 —— 验证状态机和参数校验（不连真 PG）。

repo 函数本身只是 SQL 包装，我们重点测：
  - change_status 的状态机合法性（在调 PG 前就抛）
  - add_member 的角色合法性
  - get_user_tenants / get_membership 等的纯函数行为
"""

import pytest
from apihub_core.errors import ApiError
from tenant import repository as repo
from tenant.models import TenantCreate, TenantUpdate


class TestStatusMachinePrecheck:
    """change_status 在调 DB 之前先校验状态转换合法性。"""

    async def test_bad_status_rejected(self, monkeypatch):
        """非法 new_status → INVALID_PARAMS。"""
        with pytest.raises(ApiError) as exc:
            await repo.change_status("t1", "frozen")
        assert exc.value.code.name == "INVALID_PARAMS"

    async def test_closed_is_terminal(self, monkeypatch):
        """已 closed 的租户不能再变状态。"""
        async def _get_tenant(tid):
            return {"id": tid, "status": "closed", "name": "x", "slug": "x",
                    "type": "internal", "tier": "standard", "metadata": {}}

        monkeypatch.setattr(repo, "get_tenant", _get_tenant)
        with pytest.raises(ApiError) as exc:
            await repo.change_status("t1", "active")
        assert exc.value.code.name == "CONFLICT"

    async def test_active_to_suspended_calls_db(self, monkeypatch):
        async def _get_tenant(tid):
            return {"id": tid, "status": "active", "name": "x", "slug": "x",
                    "type": "internal", "tier": "standard", "metadata": {}}

        called = {}

        class _FakeCtx:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def transaction(self):
                return self

            async def start(self):
                pass

            async def commit(self):
                pass

            async def rollback(self):
                pass

            async def execute(self, *args, **kwargs):
                return "SET"

            async def fetchrow(self, *args):
                called["fetchrow"] = args
                return {"id": "t1", "status": "suspended", "name": "x",
                        "slug": "x", "type": "internal", "tier": "standard",
                        "metadata": {}, "parent_id": None,
                        "created_at": "2026-07-01", "updated_at": "2026-07-01"}

        from apihub_core import db as db_mod

        class _FakePool:
            def __init__(self):
                self._conn = _FakeCtx()

            def acquire(self):
                return self._conn

        monkeypatch.setattr(db_mod, "_pool", _FakePool())
        monkeypatch.setattr(repo, "get_tenant", _get_tenant)

        result = await repo.change_status("t1", "suspended")
        assert result["status"] == "suspended"
        assert "fetchrow" in called


class TestRoleValidation:
    async def test_add_member_bad_role(self):
        with pytest.raises(ApiError):
            await repo.add_member("t1", "u1", "superuser")

    async def test_update_member_bad_role(self):
        with pytest.raises(ApiError):
            await repo.update_member_role("t1", "u1", "guest")


class TestTenantCreate:
    def test_normalized_type_bad_falls_back(self):
        payload = TenantCreate(
            id="t1", name="xx", slug="xx", type="garbage", tier="standard"
        )
        assert payload.normalized_type() == "internal"

    def test_normalized_tier_bad_falls_back(self):
        payload = TenantCreate(
            id="t1", name="xx", slug="xx", type="internal", tier="platinum"
        )
        assert payload.normalized_tier() == "standard"

    def test_id_pattern_rejects_spaces(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            TenantCreate(id="has space", name="x", slug="x")


class TestTenantUpdate:
    async def test_update_with_empty_payload_returns_current(self, monkeypatch):
        """空 payload 直接返回当前 tenant，不打 DB UPDATE。"""
        called = {"get_tenant": False}

        async def _get_tenant(tid):
            called["get_tenant"] = True
            return {"id": tid, "name": "n", "slug": "s", "type": "internal",
                    "status": "active", "tier": "standard", "metadata": {}}

        monkeypatch.setattr(repo, "get_tenant", _get_tenant)

        result = await repo.update_tenant("t1", TenantUpdate())
        assert called["get_tenant"] is True
        assert result["id"] == "t1"

    async def test_update_bad_tier(self):
        with pytest.raises(ApiError):
            await repo.update_tenant("t1", TenantUpdate(tier="platinum"))
