"""共享 fixtures（api-registry tests）。"""

import os

# 必须在 import apihub_core 之前注入最小 env。
_ENV_DEFAULTS = {
    "PG_HOST": "localhost",
    "PG_USER": "apihub",
    "PG_PASSWORD": "test",
    "REDIS_HOST": "localhost",
    "REDIS_PORT": "6379",
    "ENV": "test",
    "OTEL_SERVICE_NAME": "api-registry-test",
}
for k, v in _ENV_DEFAULTS.items():
    os.environ.setdefault(k, v)

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from apihub_core.config import get_settings  # noqa: E402
from apihub_core.tenant import (  # noqa: E402
    TenantContext,
    clear_tenant_context,
    set_tenant_context,
)


@pytest.fixture(autouse=True)
def reset_state():
    clear_tenant_context()
    get_settings.cache_clear()
    yield
    clear_tenant_context()
    get_settings.cache_clear()


@pytest.fixture
def stub_cr(monkeypatch):
    """覆盖 change_request 模块的 PG 操作。"""
    state = {
        "next_id": [1],
        "requests": {},  # id → ChangeRequest
        "dingtalk_returns": {},  # env → approval_id
        "applied": [],
        "apply_summary": "ok",
        "apply_raises": None,
    }

    def _make_req(req_payload):
        from datetime import UTC, datetime

        from api_registry.change_request import ChangeRequest, ChangeRequestStatus
        rid = state["next_id"][0]
        state["next_id"][0] += 1
        return ChangeRequest(
            id=rid,
            tenant_id="42",
            api_id=req_payload.api_id,
            target_version=req_payload.target_version,
            change_type=req_payload.change_type,
            target_env=req_payload.target_env,
            proposed_config=req_payload.proposed_config,
            status=ChangeRequestStatus.APPROVED
            if req_payload.target_env.value == "dev"
            else ChangeRequestStatus.PENDING,
            dingtalk_approval_id=state["dingtalk_returns"].get(req_payload.target_env.value),
            submitted_by=req_payload.submitted_by,
            submitted_at=datetime.now(UTC),
        )

    async def _submit(*, tenant_id, req, **kwargs):
        cr_obj = _make_req(req)
        state["requests"][cr_obj.id] = cr_obj
        return cr_obj.id

    async def _get(rid):
        return state["requests"].get(rid)

    async def _list(query):
        return list(state["requests"].values())

    async def _approve(rid, **kwargs):
        from api_registry.change_request import ChangeRequestStatus
        cr_obj = state["requests"].get(rid)
        if cr_obj is None or cr_obj.status != ChangeRequestStatus.PENDING:
            return False
        cr_obj.status = ChangeRequestStatus.APPROVED
        return True

    async def _reject(rid, **kwargs):
        from api_registry.change_request import ChangeRequestStatus
        cr_obj = state["requests"].get(rid)
        if cr_obj is None or cr_obj.status != ChangeRequestStatus.PENDING:
            return False
        cr_obj.status = ChangeRequestStatus.REJECTED
        return True

    async def _cancel(rid, **kwargs):
        from api_registry.change_request import ChangeRequestStatus
        cr_obj = state["requests"].get(rid)
        if cr_obj is None or cr_obj.status != ChangeRequestStatus.PENDING:
            return False
        cr_obj.status = ChangeRequestStatus.CANCELLED
        return True

    async def _mark_applied(rid):
        from api_registry.change_request import ChangeRequestStatus
        cr_obj = state["requests"].get(rid)
        if cr_obj is None or cr_obj.status != ChangeRequestStatus.APPROVED:
            return False
        cr_obj.status = ChangeRequestStatus.APPLIED
        return True

    async def _dingtalk(req):
        return state["dingtalk_returns"].get(req.target_env.value)

    async def _apply(req):
        if state["apply_raises"]:
            raise state["apply_raises"]
        state["applied"].append(req.id)
        await _mark_applied(req.id)
        return state["apply_summary"]

    from api_registry import change_request as cr_mod
    monkeypatch.setattr(cr_mod, "submit_change_request", _submit)
    monkeypatch.setattr(cr_mod, "get_change_request", _get)
    monkeypatch.setattr(cr_mod, "list_change_requests", _list)
    monkeypatch.setattr(cr_mod, "approve_change_request", _approve)
    monkeypatch.setattr(cr_mod, "reject_change_request", _reject)
    monkeypatch.setattr(cr_mod, "cancel_change_request", _cancel)
    monkeypatch.setattr(cr_mod, "mark_applied", _mark_applied)
    monkeypatch.setattr(cr_mod, "submit_dingtalk_approval", _dingtalk)
    monkeypatch.setattr(cr_mod, "apply_change", _apply)
    return state


@pytest.fixture
def stub_kafka(monkeypatch):
    """吞掉 kafka.emit。"""
    events = []

    async def _noop(topic, payload, **kwargs):
        events.append((topic, payload))

    from apihub_core import kafka as k_mod
    monkeypatch.setattr(k_mod, "emit", _noop)
    return events


@pytest_asyncio.fixture
async def admin_client(monkeypatch, stub_kafka):
    """超管上下文 client。"""
    from apihub_core import auth as auth_mod

    ctx = TenantContext(
        tenant_id="42",
        tenant_type="platform",
        user_id="u_admin",
        is_platform_admin=True,
    )

    async def _noop_auth(request, settings, api_key):  # noqa: ARG001
        set_tenant_context(ctx)
        return ctx

    monkeypatch.setattr(auth_mod, "authenticate_request", _noop_auth)

    import httpx
    from api_registry.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"X-API-Key": "ak_test"},
    ) as c:
        yield c


@pytest_asyncio.fixture
async def normal_client(monkeypatch, stub_kafka):
    """普通用户 client（非超管）。"""
    from apihub_core import auth as auth_mod

    ctx = TenantContext(
        tenant_id="42",
        tenant_type="internal",
        user_id="u_bob",
        is_platform_admin=False,
    )

    async def _noop_auth(request, settings, api_key):  # noqa: ARG001
        set_tenant_context(ctx)
        return ctx

    monkeypatch.setattr(auth_mod, "authenticate_request", _noop_auth)

    import httpx
    from api_registry.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"X-API-Key": "ak_test"},
    ) as c:
        yield c
