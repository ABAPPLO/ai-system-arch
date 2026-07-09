"""Argo client 测试 —— stub 模式覆盖完整状态机。"""

import pytest


@pytest.fixture
async def stub_client():
    from workflow_svc.argo_client import StubArgoClient

    c = StubArgoClient()
    yield c
    await c.close()


class TestStubSubmit:
    async def test_submit_returns_argo_name(self, stub_client):
        name = await stub_client.submit(
            namespace="apihub-workflows",
            workflow_uuid="uuid-abc123",
            spec={
                "entrypoint": "main",
                "templates": [
                    {"name": "main", "script": {"image": "python:3.11"}},
                ],
            },
            labels={"tenant_id": "42", "api_id": "100"},
        )
        assert name.startswith("wf-uuid-abc")
        # name 末尾有 counter
        assert name.split("-")[-1].isdigit()

    async def test_submit_with_no_templates(self, stub_client):
        """spec 没 templates 也不应炸（step 列表为空）。"""
        name = await stub_client.submit(
            namespace="ns",
            workflow_uuid="uuid-xyz",
            spec={"entrypoint": "main"},
            labels={},
        )
        assert name.startswith("wf-uuid-xyz")
        # 内部状态
        steps = await stub_client.get_steps(namespace="ns", argo_name=name)
        assert steps == []


class TestStubStatus:
    async def test_initial_status_running(self, stub_client):
        name = await stub_client.submit(
            namespace="ns",
            workflow_uuid="u1",
            spec={"templates": [{"name": "step1"}]},
            labels={},
        )
        status, msg = await stub_client.get_status(namespace="ns", argo_name=name)
        assert status.value == "running"
        assert msg is None

    async def test_get_status_not_found(self, stub_client):
        from workflow_svc.argo_client import ArgoError

        with pytest.raises(ArgoError):
            await stub_client.get_status(namespace="ns", argo_name="nope")


class TestStubCancel:
    async def test_cancel_sets_status(self, stub_client):
        from workflow_svc.models import WorkflowStatus

        name = await stub_client.submit(
            namespace="ns",
            workflow_uuid="u2",
            spec={"templates": [{"name": "s1"}]},
            labels={},
        )
        await stub_client.cancel(namespace="ns", argo_name=name)
        status, _ = await stub_client.get_status(namespace="ns", argo_name=name)
        assert status == WorkflowStatus.CANCELLED

    async def test_resume_after_cancel(self, stub_client):
        from workflow_svc.models import WorkflowStatus

        name = await stub_client.submit(
            namespace="ns",
            workflow_uuid="u3",
            spec={"templates": [{"name": "s1"}]},
            labels={},
        )
        await stub_client.cancel(namespace="ns", argo_name=name)
        await stub_client.resume(namespace="ns", argo_name=name)
        status, _ = await stub_client.get_status(namespace="ns", argo_name=name)
        assert status == WorkflowStatus.RUNNING


class TestStubLogs:
    async def test_stream_logs_yields_lines(self, stub_client):
        name = await stub_client.submit(
            namespace="ns",
            workflow_uuid="u4",
            spec={"templates": [{"name": "step1"}, {"name": "step2"}]},
            labels={},
        )
        lines = []
        async for line in stub_client.stream_logs(namespace="ns", argo_name=name):
            lines.append(line)
        # 至少能拽到 step1 的初始日志行
        assert any("step1" in line for line in lines)

    async def test_stream_logs_single_step(self, stub_client):
        name = await stub_client.submit(
            namespace="ns",
            workflow_uuid="u5",
            spec={"templates": [{"name": "solo"}]},
            labels={},
        )
        lines = []
        async for line in stub_client.stream_logs(namespace="ns", argo_name=name, step_name="solo"):
            lines.append(line)
        assert len(lines) >= 1
        assert "solo" in lines[0]


class TestFactory:
    async def test_init_stub(self):
        from workflow_svc import argo_client

        c = argo_client.init_argo_client(mode="stub")
        assert isinstance(c, argo_client.StubArgoClient)
        assert argo_client.get_argo_client() is c
        await argo_client.close_argo_client()

    def test_init_unknown_mode(self):
        from workflow_svc import argo_client

        with pytest.raises(ValueError, match="unknown argo_mode"):
            argo_client.init_argo_client(mode="bogus")

    def test_get_uninitialized(self):
        from workflow_svc import argo_client

        argo_client._client = None
        with pytest.raises(RuntimeError, match="not initialized"):
            argo_client.get_argo_client()


class TestPhaseToStatus:
    def test_phase_mapping(self):
        from workflow_svc.argo_client import _phase_to_status
        from workflow_svc.models import WorkflowStatus

        assert _phase_to_status("Running") == WorkflowStatus.RUNNING
        assert _phase_to_status("Succeeded") == WorkflowStatus.SUCCEEDED
        assert _phase_to_status("Failed") == WorkflowStatus.FAILED
        assert _phase_to_status("Error") == WorkflowStatus.FAILED
        assert _phase_to_status("Stopped") == WorkflowStatus.CANCELLED
        assert _phase_to_status("Skipped") == WorkflowStatus.CANCELLED
        assert _phase_to_status("Pending") == WorkflowStatus.SUBMITTED
        assert _phase_to_status("Garbage") == WorkflowStatus.UNKNOWN
