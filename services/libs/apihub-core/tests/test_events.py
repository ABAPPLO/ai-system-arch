"""R0b: Kafka 事件契约 round-trip + 容忍多余字段。纯单测，无 PG/Redis/Kafka。"""

from dataclasses import FrozenInstanceError, asdict

import pytest
from apihub_core import kafka  # noqa: F401  (确保可 import；Task 2 才加 parse_event)
from apihub_core.events import CallEvent, TaskFailure, TaskRequest, TaskStatus


def _call_event(**over):
    base = {
        "ts": "2026-07-15 10:00:00.000",
        "tenant_id": "t1",
        "tenant_type": "internal",
        "app_id": "a1",
        "api_id": "api1",
        "api_version_id": "v1",
        "trace_id": "trc_1",
        "request_id": "req_1",
        "method": "GET",
        "path": "/x",
        "status_code": 200,
        "is_success": 1,
        "latency_ms": 5,
        "request_size": 10,
        "response_size": 20,
    }
    base.update(over)
    return CallEvent(**base)


@pytest.mark.parametrize(
    "evt",
    [
        TaskRequest(task_id="tk_1", api_id="api1", api_version_id="v1", backend_url="http://b"),
        TaskStatus(task_id="tk_1", tenant_id="t1", app_id="a1", api_id="api1", status="succeeded"),
        TaskFailure(task_id="tk_1", tenant_id="t1", api_id="api1", backend_url="http://b"),
        _call_event(),
    ],
)
def test_topic_constant(evt):
    assert evt.TOPIC in {"task-requests", "task-status", "task-failures", "api-call-events"}


def test_call_event_int_fields_not_bool():
    e = _call_event(is_success=0, ai_streaming=1)
    assert isinstance(e.is_success, int) and isinstance(e.ai_streaming, int)


def test_from_dict_roundtrip():
    e = _call_event(error_code="backend_timeout")
    assert CallEvent.from_dict(asdict(e)) == e


def test_from_dict_ignores_extra_fields():
    e = TaskStatus(task_id="tk", tenant_id="t", app_id="a", api_id="api", status="failed")
    payload = {**asdict(e), "unknown_future_col": "x", "another": 123}
    assert TaskStatus.from_dict(payload) == e  # 多余字段被忽略，不抛


def test_from_dict_missing_required_raises():
    with pytest.raises(TypeError):
        TaskRequest.from_dict({"api_id": "api1"})  # 缺 task_id/backend_url 等


def test_frozen():
    e = TaskStatus(task_id="tk", tenant_id="t", app_id="a", api_id="api", status="failed")
    with pytest.raises(FrozenInstanceError):
        e.status = "succeeded"  # frozen


from apihub_core import kafka as core_kafka


def test_parse_event_routes_by_topic():
    payload = {
        "task_id": "tk",
        "tenant_id": "t",
        "app_id": "a",
        "api_id": "api",
        "status": "failed",
        "future_col": "ignored",
    }
    evt = core_kafka.parse_event("task-status", payload)
    assert isinstance(evt, TaskStatus)
    assert evt.status == "failed"


def test_parse_event_unknown_topic_raises():
    import pytest

    with pytest.raises(ValueError):
        core_kafka.parse_event("nope-topic", {"a": 1})


def test_emit_event_requires_topic():
    # emit_event 取 event.TOPIC；无 TOPIC 的对象应拒
    import asyncio

    import pytest

    class NoTopic:
        pass

    with pytest.raises(TypeError):
        asyncio.run(core_kafka.emit_event(NoTopic()))
