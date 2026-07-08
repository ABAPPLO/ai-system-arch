"""共享 fixtures（workflow tests）。"""

import os

# 必须在 import apihub_core 之前注入最小 env，避免 Settings 校验炸。
_ENV_DEFAULTS = {
    "PG_HOST": "localhost",
    "PG_USER": "apihub",
    "PG_PASSWORD": "test",
    "REDIS_HOST": "localhost",
    "REDIS_PORT": "6379",
    "ENV": "test",
    "ARGO_MODE": "stub",
    "OTEL_SERVICE_NAME": "workflow-test",
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
def stub_repo(monkeypatch):
    """覆盖 repository.* 为可控 spy，避免依赖真 PG。"""
    state = {
        "workflows": {},  # id → WorkflowDetail
        "list_returns": [],
    }
    next_id = [1]

    async def _create(**kwargs):
        from datetime import UTC, datetime

        from workflow_svc.models import WorkflowDetail, WorkflowStatus
        wf_id = next_id[0]
        next_id[0] += 1
        wf = WorkflowDetail(
            id=wf_id,
            tenant_id=kwargs["tenant_id"],
            workflow_uuid=kwargs["workflow_uuid"],
            argo_name=kwargs["argo_name"],
            namespace=kwargs["namespace"],
            api_id=kwargs["api_id"],
            app_id=kwargs["app_id"],
            trace_id=kwargs["trace_id"],
            spec=kwargs["spec"],
            status=kwargs.get("status", WorkflowStatus.RUNNING),
            submitted_at=datetime.now(UTC),
            finished_at=None,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        state["workflows"][wf_id] = wf
        return wf_id

    async def _get(wf_id):
        return state["workflows"].get(wf_id)

    async def _get_by_uuid(uuid):
        return None  # 测试用不到

    async def _list(q):
        return state["list_returns"]

    async def _update_status(wf_id, **kwargs):
        wf = state["workflows"].get(wf_id)
        if wf is None:
            return False
        wf.status = kwargs["status"]
        if "message" in kwargs:
            wf.message = kwargs["message"]
        if "finished_at" in kwargs:
            wf.finished_at = kwargs["finished_at"]
        return True

    from workflow_svc import repository as repo
    monkeypatch.setattr(repo, "create_workflow", _create)
    monkeypatch.setattr(repo, "get_workflow", _get)
    monkeypatch.setattr(repo, "get_workflow_by_uuid", _get_by_uuid)
    monkeypatch.setattr(repo, "list_workflows", _list)
    monkeypatch.setattr(repo, "update_status", _update_status)
    return state


@pytest_asyncio.fixture
async def client(monkeypatch, stub_repo):
    """httpx ASGITransport client，绕过鉴权 + init stub argo client。"""
    from apihub_core import auth as auth_mod
    from workflow_svc import argo_client

    argo_client.init_argo_client(mode="stub")

    ctx = TenantContext(
        tenant_id="42",
        tenant_type="platform",
        app_id="",
        is_platform_admin=True,
    )

    async def _noop_auth(request, settings, api_key):  # noqa: ARG001
        set_tenant_context(ctx)
        return ctx

    monkeypatch.setattr(auth_mod, "authenticate_request", _noop_auth)

    import httpx
    from workflow_svc.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"X-API-Key": "ak_test"},
    ) as c:
        yield c

    await argo_client.close_argo_client()
