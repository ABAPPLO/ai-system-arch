"""consumer 测试 —— 用 fake Kafka queue 验证消息流转。

不依赖真 Kafka，验证：
  - 消息能从 queue 拽到 process_task
  - tenant_id / app_id / trace_id 从 header 回填，request_id 从 payload（typed 契约）
  - 单条消息异常不杀 worker
  - stop 信号能优雅退出
"""

import asyncio

import pytest


class _FakeKafkaMsg:
    def __init__(self, value, key=None, headers=None):
        self.value = value
        self.key = key
        self.headers = headers or []


class _FakeConsumer:
    """模拟 aiokafka AIOKafkaConsumer，提供 async iter + commit。"""

    def __init__(self, messages):
        self._messages = messages
        self.commits = 0
        self.started = False

    async def start(self):
        self.started = True

    async def stop(self):
        self.started = False

    def __aiter__(self):
        self._iter = iter(self._messages)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            # 模拟 Kafka 长连：等一会儿再抛 StopAsyncIteration
            await asyncio.sleep(0.01)
            raise StopAsyncIteration from None

    async def commit(self):
        self.commits += 1


@pytest.fixture
def captured_tasks():
    """记录所有传给 process_task 的 TaskRequest。"""
    return []


@pytest.fixture
def stub_processor(monkeypatch, captured_tasks):
    """把 process_task 换成 spy。"""
    from executor import consumer as mod

    async def _stub(msg):
        captured_tasks.append(msg)

    monkeypatch.setattr(mod, "process_task", _stub)


@pytest.fixture
def fake_settings():
    from apihub_core.config import Settings

    return Settings(
        pg_host="localhost",
        pg_user="apihub",
        pg_password="test",  # noqa: S106
        redis_host="localhost",
        kafka_brokers="localhost:9092",
    )


class TestHandle:
    async def test_parses_message_and_headers(self, fake_settings, stub_processor, captured_tasks):
        """Kafka 消息体 + header → TaskRequest 字段齐全。

        request_id 走 payload（Task 3 闭合通道：生产侧 TaskRequest 已带 request_id）；
        tenant_id / app_id / trace_id 仍由 kafka.emit 注入 header，消费侧重填。
        """
        from executor.consumer import TaskConsumer

        msg = _FakeKafkaMsg(
            value={
                "task_id": "task_xyz12345",
                "api_id": "api_a",
                "api_version_id": "ver_1",
                "backend_url": "http://b/h",
                "payload": '{"x":1}',
                "timeout_seconds": 45.0,
                "request_id": "req_99",
            },
            key="task_xyz12345",
            headers=[
                (b"tenant_id", b"t_99"),
                (b"app_id", b"app_99"),
            ],
        )

        c = TaskConsumer(fake_settings)
        await c._handle(msg)

        assert len(captured_tasks) == 1
        m = captured_tasks[0]
        assert m.task_id == "task_xyz12345"
        assert m.backend_url == "http://b/h"
        assert m.timeout_seconds == 45.0
        assert m.tenant_id == "t_99"
        assert m.app_id == "app_99"
        assert m.request_id == "req_99"

    async def test_missing_headers_tolerated(self, fake_settings, stub_processor, captured_tasks):
        """没 header（异常情况）也不应崩。"""
        from executor.consumer import TaskConsumer

        msg = _FakeKafkaMsg(
            value={
                "task_id": "task_nohdr1234",
                "api_id": "",
                "api_version_id": "",
                "backend_url": "http://b/h",
            },
        )

        c = TaskConsumer(fake_settings)
        await c._handle(msg)  # 不应抛

        assert captured_tasks[0].tenant_id is None
        assert captured_tasks[0].request_id is None


class TestRun:
    async def test_commits_after_each_message(
        self, fake_settings, stub_processor, captured_tasks, monkeypatch
    ):
        """每条消息处理完应 commit。"""
        from executor import consumer as mod

        fake = _FakeConsumer(
            [
                _FakeKafkaMsg(
                    value={
                        "task_id": "task_aaaaaaaa",
                        "api_id": "",
                        "api_version_id": "",
                        "backend_url": "http://b/1",
                    }
                ),
                _FakeKafkaMsg(
                    value={
                        "task_id": "task_bbbbbbbb",
                        "api_id": "",
                        "api_version_id": "",
                        "backend_url": "http://b/2",
                    }
                ),
            ]
        )

        c = mod.TaskConsumer(fake_settings)
        c._consumer = fake

        # 跑一小段让两条消息都拽完，然后停
        run_task = asyncio.create_task(c._run())
        await asyncio.sleep(0.1)
        c._stop.set()
        try:
            await asyncio.wait_for(run_task, timeout=1.0)
        except TimeoutError:
            run_task.cancel()

        assert fake.commits == 2
        assert len(captured_tasks) == 2

    async def test_processor_exception_doesnt_crash_loop(self, fake_settings, monkeypatch):
        """process_task 抛异常 → 记 log + 继续拽下条 + 仍 commit。"""
        from executor import consumer as mod

        call_count = 0

        async def _flaky(msg):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("simulated")

        monkeypatch.setattr(mod, "process_task", _flaky)

        fake = _FakeConsumer(
            [
                _FakeKafkaMsg(
                    value={
                        "task_id": "task_aaaaaaaa",
                        "api_id": "",
                        "api_version_id": "",
                        "backend_url": "http://b/1",
                    }
                ),
                _FakeKafkaMsg(
                    value={
                        "task_id": "task_bbbbbbbb",
                        "api_id": "",
                        "api_version_id": "",
                        "backend_url": "http://b/2",
                    }
                ),
            ]
        )
        c = mod.TaskConsumer(fake_settings)
        c._consumer = fake

        run_task = asyncio.create_task(c._run())
        await asyncio.sleep(0.15)
        c._stop.set()
        try:
            await asyncio.wait_for(run_task, timeout=1.0)
        except TimeoutError:
            run_task.cancel()

        # 两条都处理了，两条都 commit 了（异常也 commit：避免反复重投同一条毒消息）
        assert call_count == 2
        assert fake.commits == 2
