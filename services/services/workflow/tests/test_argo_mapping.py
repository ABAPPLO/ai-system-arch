"""K8sArgoClient 的 phase/node 映射单测（CI，不依赖 kind）。"""

from workflow_svc.argo_client import _node_to_step, _params_to_dict, _phase_to_status
from workflow_svc.models import StepStatus, WorkflowStatus


def test_phase_to_status_known():
    assert _phase_to_status("Succeeded") is WorkflowStatus.SUCCEEDED
    assert _phase_to_status("Running") is WorkflowStatus.RUNNING
    assert _phase_to_status("Failed") is WorkflowStatus.FAILED
    assert _phase_to_status("Error") is WorkflowStatus.FAILED
    assert _phase_to_status("Stopped") is WorkflowStatus.CANCELLED
    assert _phase_to_status("Skipped") is WorkflowStatus.CANCELLED
    assert _phase_to_status("Pending") is WorkflowStatus.SUBMITTED


def test_phase_to_status_unknown():
    assert _phase_to_status("") is WorkflowStatus.UNKNOWN
    assert _phase_to_status("WeirdPhase") is WorkflowStatus.UNKNOWN


def test_params_to_dict():
    params = [{"name": "a", "value": "1"}, {"name": "b", "value": "x"}]
    assert _params_to_dict(params) == {"a": "1", "b": "x"}
    assert _params_to_dict(None) == {}
    assert _params_to_dict([]) == {}


def test_node_to_step_maps_fields_and_inputs_as_dict():
    node = {
        "name": "wf-abc123[0].s1",
        "phase": "Succeeded",
        "templateName": "echo",
        "startedAt": "2026-07-10T10:00:00Z",
        "finishedAt": "2026-07-10T10:00:05Z",
        "message": "ok",
        "inputs": {"parameters": [{"name": "p", "value": "v"}]},
        "outputs": {"parameters": [{"name": "o", "value": "w"}]},
    }
    step = _node_to_step(node)
    assert step.name == "wf-abc123[0].s1"
    assert step.template == "echo"
    assert step.status is StepStatus.SUCCEEDED
    assert step.started_at is not None and step.started_at.year == 2026
    assert step.finished_at is not None
    assert step.message == "ok"
    # 关键：inputs/outputs 必须是 dict（Argo parameters list → dict），否则 pydantic 炸
    assert step.inputs == {"p": "v"}
    assert step.outputs == {"o": "w"}


def test_node_to_step_missing_fields_safe():
    step = _node_to_step({"name": "n", "phase": "Running"})
    assert step.name == "n"
    assert step.status is StepStatus.RUNNING
    assert step.started_at is None
    assert step.inputs == {}
    assert step.outputs == {}
