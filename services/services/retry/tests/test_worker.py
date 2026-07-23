"""worker 测试 —— ZSet poll → executor → PG 状态更新。"""

import pytest


class _FakeExecutorClient:
    """模拟 httpx.AsyncClient。"""

    def __init__(self, *, status_code=200, body=None, raise_error=None):
        self._status = status_code
        self._body = body or {"succeeded": True}
        self._raise = raise_error

    async def post(self, url, json=None, timeout=None):  # noqa: ARG002, ASYNC109
        if self._raise:
            raise self._raise

        class _Resp:
            def __init__(self, status, body):
                self.status_code = status
                self._body = body
                self.text = str(body)

            def json(self):
                return self._body

        return _Resp(self._status, self._body)

    async def aclose(self):
        pass


@pytest.fixture
def worker_state(monkeypatch):
    """记录 worker 调用的 repo 方法 + delay_queue.schedule。"""
    state = {
        "mark_started": [],
        "mark_succeeded": [],
        "mark_failed": [],
        "schedules": [],
        "pop_due_returns": [],
        "tenants_returned": [42],
    }

    async def _mark_started(rid):
        state["mark_started"].append(rid)
        return True

    async def _mark_succeeded(rid, **kwargs):
        state["mark_succeeded"].append((rid, kwargs))

    async def _mark_failed(rid, **kwargs):
        state["mark_failed"].append((rid, kwargs))

    async def _schedule(**kwargs):
        state["schedules"].append(kwargs)

    async def _pop_due(*, tenant_id, max_count=10, now_ts=None):  # noqa: ARG002
        if state["pop_due_returns"]:
            return state["pop_due_returns"].pop(0)
        return []

    async def _list_tenants():
        return state["tenants_returned"]

    from retry_svc import delay_queue
    from retry_svc import repository as repo

    monkeypatch.setattr(repo, "mark_attempt_started", _mark_started)
    monkeypatch.setattr(repo, "mark_succeeded", _mark_succeeded)
    monkeypatch.setattr(repo, "mark_failed_attempt", _mark_failed)
    monkeypatch.setattr(delay_queue, "schedule", _schedule)
    monkeypatch.setattr(delay_queue, "pop_due", _pop_due)
    monkeypatch.setattr(delay_queue, "list_tenants_with_pending", _list_tenants)
    monkeypatch.setattr(delay_queue, "complete", _noop_complete)
    return state


async def _noop_complete(*, tenant_id, retry_task_id):  # noqa: ARG001
    pass


@pytest.fixture
def fake_detail_factory(monkeypatch):
    """让 repo.get_retry_task 返回受控 detail。"""
    details = {}

    async def _get(rid):
        return details.get(rid)

    from retry_svc import repository as repo

    monkeypatch.setattr(repo, "get_retry_task", _get)
    return details


class TestExecuteOne:
    async def test_succeeded(self, monkeypatch, worker_state, fake_detail_factory):
        """executor 200 → mark_succeeded。"""
        from datetime import datetime

        from retry_svc import worker as wmod
        from retry_svc.models import (
            BackoffPolicy,
            RetryStatus,
            RetryTaskDetail,
        )

        detail = RetryTaskDetail(
            id=1,
            tenant_id=42,
            trace_id="tr_x",
            api_id=100,
            app_id=200,
            max_attempts=3,
            retry_count=0,
            next_retry_at=None,
            backoff_policy=BackoffPolicy.EXPONENTIAL,
            backoff_base_ms=1000,
            status=RetryStatus.PENDING,
            env="test",
            created_at=datetime(2026, 7, 1),
            updated_at=datetime(2026, 7, 1),
            original_request={"task_id": "task_abc", "backend_url": "http://b/h"},
        )
        fake_detail_factory[1] = detail

        worker = wmod.RetryWorker()
        worker._client = _FakeExecutorClient(status_code=200, body={"succeeded": True})

        await worker._execute_one(tenant_id=42, retry_task_id=1)

        assert worker_state["mark_started"] == [1]
        assert len(worker_state["mark_succeeded"]) == 1
        rid, kwargs = worker_state["mark_succeeded"][0]
        assert rid == 1
        assert kwargs["response_status"] == 200
        assert worker_state["mark_failed"] == []
        assert worker_state["schedules"] == []

    async def test_failed_rescheduled(self, monkeypatch, worker_state, fake_detail_factory):
        """executor 500 + retry_count < max → mark_failed_attempt + schedule。"""
        from datetime import datetime

        from retry_svc import worker as wmod
        from retry_svc.models import (
            BackoffPolicy,
            RetryStatus,
            RetryTaskDetail,
        )

        detail = RetryTaskDetail(
            id=2,
            tenant_id=42,
            trace_id="tr_x",
            api_id=100,
            app_id=200,
            max_attempts=3,
            retry_count=0,
            next_retry_at=None,
            backoff_policy=BackoffPolicy.EXPONENTIAL,
            backoff_base_ms=1000,
            status=RetryStatus.PENDING,
            env="test",
            created_at=datetime(2026, 7, 1),
            updated_at=datetime(2026, 7, 1),
            original_request={"task_id": "task_abc", "backend_url": "http://b/h"},
        )
        fake_detail_factory[2] = detail

        worker = wmod.RetryWorker()
        worker._client = _FakeExecutorClient(status_code=500, body={"err": "x"})

        await worker._execute_one(tenant_id=42, retry_task_id=2)

        assert worker_state["mark_succeeded"] == []
        assert len(worker_state["mark_failed"]) == 1
        rid, kwargs = worker_state["mark_failed"][0]
        assert rid == 2
        assert kwargs["next_retry_at"] is not None  # 还有重试机会
        assert len(worker_state["schedules"]) == 1

    async def test_failed_to_dead_letter(self, monkeypatch, worker_state, fake_detail_factory):
        """retry_count + 1 > max_attempts → mark_failed_attempt(next_retry_at=None)。"""
        from datetime import datetime

        from retry_svc import worker as wmod
        from retry_svc.models import (
            BackoffPolicy,
            RetryStatus,
            RetryTaskDetail,
        )

        detail = RetryTaskDetail(
            id=3,
            tenant_id=42,
            trace_id="tr_x",
            api_id=100,
            app_id=200,
            max_attempts=3,
            retry_count=3,
            next_retry_at=None,
            backoff_policy=BackoffPolicy.EXPONENTIAL,
            backoff_base_ms=1000,
            status=RetryStatus.PENDING,
            env="test",
            created_at=datetime(2026, 7, 1),
            updated_at=datetime(2026, 7, 1),
            original_request={"task_id": "task_abc", "backend_url": "http://b/h"},
        )
        fake_detail_factory[3] = detail

        worker = wmod.RetryWorker()
        worker._client = _FakeExecutorClient(status_code=500, body={"err": "x"})

        await worker._execute_one(tenant_id=42, retry_task_id=3)

        assert worker_state["mark_succeeded"] == []
        assert len(worker_state["mark_failed"]) == 1
        rid, kwargs = worker_state["mark_failed"][0]
        assert rid == 3
        assert kwargs["next_retry_at"] is None  # 进死信
        assert worker_state["schedules"] == []

    async def test_skip_if_not_pending(self, monkeypatch, worker_state, fake_detail_factory):
        """mark_attempt_started 返回 False（其他 worker 已抢）→ 跳过。"""
        from retry_svc import worker as wmod

        worker_state["mark_started_returns"] = False
        from retry_svc import repository as repo

        async def _false(rid):
            worker_state["mark_started"].append(rid)
            return False

        monkeypatch.setattr(repo, "mark_attempt_started", _false)

        worker = wmod.RetryWorker()
        worker._client = _FakeExecutorClient(status_code=200)
        await worker._execute_one(tenant_id=42, retry_task_id=99)

        assert worker_state["mark_succeeded"] == []
        assert worker_state["mark_failed"] == []

    async def test_executor_timeout(self, monkeypatch, worker_state, fake_detail_factory):
        """httpx.TimeoutException → 视为失败，按重试逻辑走。"""
        from datetime import datetime

        import httpx
        from retry_svc import worker as wmod
        from retry_svc.models import (
            BackoffPolicy,
            RetryStatus,
            RetryTaskDetail,
        )

        detail = RetryTaskDetail(
            id=4,
            tenant_id=42,
            trace_id="tr_x",
            api_id=100,
            app_id=200,
            max_attempts=3,
            retry_count=0,
            next_retry_at=None,
            backoff_policy=BackoffPolicy.EXPONENTIAL,
            backoff_base_ms=1000,
            status=RetryStatus.PENDING,
            env="test",
            created_at=datetime(2026, 7, 1),
            updated_at=datetime(2026, 7, 1),
            original_request={"task_id": "task_abc", "backend_url": "http://b/h"},
        )
        fake_detail_factory[4] = detail

        worker = wmod.RetryWorker()
        worker._client = _FakeExecutorClient(raise_error=httpx.TimeoutException("timeout"))

        await worker._execute_one(tenant_id=42, retry_task_id=4)

        assert len(worker_state["mark_failed"]) == 1
        rid, kwargs = worker_state["mark_failed"][0]
        assert kwargs["error_code"] == "executor_timeout"


class TestTick:
    async def test_tick_processes_tenant(self, monkeypatch, worker_state, fake_detail_factory):
        """完整 tick：list_tenants → pop_due → execute_one。"""
        from datetime import datetime

        from retry_svc import worker as wmod
        from retry_svc.models import (
            BackoffPolicy,
            RetryStatus,
            RetryTaskDetail,
        )

        detail = RetryTaskDetail(
            id=10,
            tenant_id=42,
            trace_id="tr_x",
            api_id=100,
            app_id=200,
            max_attempts=3,
            retry_count=0,
            next_retry_at=None,
            backoff_policy=BackoffPolicy.EXPONENTIAL,
            backoff_base_ms=1000,
            status=RetryStatus.PENDING,
            env="test",
            created_at=datetime(2026, 7, 1),
            updated_at=datetime(2026, 7, 1),
            original_request={"task_id": "task_abc", "backend_url": "http://b/h"},
        )
        fake_detail_factory[10] = detail
        worker_state["pop_due_returns"] = [[10]]

        worker = wmod.RetryWorker()
        worker._client = _FakeExecutorClient(status_code=200)
        await worker._tick()

        assert worker_state["mark_started"] == [10]
        assert len(worker_state["mark_succeeded"]) == 1

    async def test_tick_no_tenants(self, worker_state):
        """没租户 → 不抛，正常结束。"""
        from retry_svc import worker as wmod

        worker_state["tenants_returned"] = []

        worker = wmod.RetryWorker()
        worker._client = _FakeExecutorClient()
        await worker._tick()  # 不抛
