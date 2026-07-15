#!/usr/bin/env python3.11
"""R1a retry-chain e2e runbook —— 从 task-requests 真实入口验证自动重试链。

⚠️ 禁止手动注入 task-failures —— 本脚本【只】投 task-requests 一条消息，
   之后完全靠观察 PG 的 retry_task 行自动出现并跑到 dead，来证明 executor 的
   task-failures 生产真闭合（R1a §2.1）。这是审计 §6 的方法论教训：测试不能
   跳过被测环节（executor → task-failures）再从下游手动灌数据"假装"链路通。

被测链路（全自动，脚本不介入中间任何环节）：
  本脚本 ──produce──> task-requests (TaskRequest, backend_url=http://127.0.0.1:9/x)
         │                     │
         │                     ▼
         │              executor 消费（consumer.py::_handle → processor.process_task）
         │                     │  mark_running(pending→running) → 调 backend
         │                     │  http://127.0.0.1:9/x 端口 9 无监听 → connect refused
         │                     ▼
         │              executor mark_failed + emit TaskFailure ──> task-failures
         │                     │  （R1a §2.1：这一跳是被验证的"断链修复点"）
         │                     ▼
         │              retry 消费（consumer.py::_handle）
         │                     │  create_retry_task(task_instance_id=task_id)  ← R1a §2.7 幂等
         │                     │  delay_queue.schedule                          ← R1a §2.2 trigger
         │                     ▼
         │              retry worker 轮询到期 → 调 executor 重试 → 仍连接拒绝
         │                     │  mark_failed_attempt × max_attempts（默认 3）
         │                     ▼
         └──轮询 PG────── retry_task: status='dead' ∧ retry_count >= max_attempts

判定逻辑：轮询 SELECT status, retry_count, max_attempts FROM retry_task
         WHERE task_instance_id = $task_id，直到 status='dead' 且
         retry_count >= max_attempts，或超时（默认 60s）。

前置（必须先就绪，脚本不负责拉起）：
  - 存储栈：PG(:15433) / Redis(:6379) / Kafka(:9094) —— make dev-up
  - executor 服务在跑（make run-executor，:8003）—— 它消费 task-requests + 产 task-failures
  - retry 服务在跑（make run-retry，:8009）—— 含 consumer（消费 task-failures）+ worker（退避调度）
  - seed 数据：tenant_a / api_demo_a / ver_demo_a_v1 / app_trading（02-seed.sql，默认即有）

环境变量（均有默认值，指向 dev 栈）：
  TEST_PG_DSN       PG 业务 DSN（apihub_app，受 RLS）  默认 localhost:15433
  KAFKA_BOOTSTRAP   Kafka bootstrap servers            默认 localhost:9094
  TENANT_ID / API_ID / API_VERSION_ID / APP_ID        默认对齐 02-seed.sql
  POLL_TIMEOUT_S    轮询超时（秒）                       默认 60
  POLL_INTERVAL_S   轮询间隔（秒）                       默认 1.0

退出码：0 = 链路闭合（retry_task 到 dead 且 retry_count 耗尽）；1 = 超时 / 写入 / 链路断。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid

import aiokafka
import asyncpg

# ---------------------------------------------------------------------------
# 配置（env 可覆盖，默认对齐 dev 栈 + 02-seed.sql）
# ---------------------------------------------------------------------------
PG_DSN = os.environ.get(
    "TEST_PG_DSN",
    "postgresql://apihub_app:apihub_app_dev_pwd@localhost:15433/apihub",
)
KAFKA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", "localhost:9094")

TENANT_ID = os.environ.get("TENANT_ID", "tenant_a")
API_ID = os.environ.get("API_ID", "api_demo_a")
API_VERSION_ID = os.environ.get("API_VERSION_ID", "ver_demo_a_v1")
APP_ID = os.environ.get("APP_ID", "app_trading")

TASK_TOPIC = "task-requests"
# 端口 9（discard/wake-on-lan 区段）本地几乎必然无监听 → connect refused，
# executor 的 httpx 调用立刻失败 → mark_failed → 产 TaskFailure。
# 不用 127.0.0.1:1（root 有时占）或 0.0.0.0（行为不确定）。
DEAD_BACKEND_URL = "http://127.0.0.1:9/x"

POLL_TIMEOUT_S = float(os.environ.get("POLL_TIMEOUT_S", "60"))
POLL_INTERVAL_S = float(os.environ.get("POLL_INTERVAL_S", "1.0"))


# ---------------------------------------------------------------------------
# PG 辅助：在 apihub_app（受 RLS）连接里临时注入 tenant_id，才能 INSERT/SELECT
# task 与 retry_task。等价于 apihub_core.db.db_session() 做的事（见 db.py）。
# 这里不 import apihub_core，保持脚本零服务依赖（仅需 aiokafka + asyncpg）。
# ---------------------------------------------------------------------------


async def insert_pending_task(conn: asyncpg.Connection, task_id: str, trace_id: str) -> None:
    """先建 pending task 行：executor.process_task 开头 mark_running(pending→running)
    是原子 UPDATE，找不到 pending 行就跳过、不会产 TaskFailure。故必须预置。

    mark_running 走 admin_db_session（绕 RLS），所以只要行存在、tenant_id 对得上
    RLS 写入策略即可被改写。
    """
    async with conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", TENANT_ID)
        await conn.execute(
            """
            INSERT INTO task (
                id, tenant_id, api_id, api_version_id, app_id, status,
                payload, request_id, trace_id
            ) VALUES ($1, $2, $3, $4, $5, 'pending', $6, $7, $8)
            ON CONFLICT (id) DO UPDATE SET status='pending', updated_at=NOW()
            """,
            task_id,
            TENANT_ID,
            API_ID,
            API_VERSION_ID,
            APP_ID,
            json.dumps({"job": "r1a-smoke", "force": "fail"}),
            f"req_{task_id}",
            trace_id,
        )


async def fetch_retry_task(conn: asyncpg.Connection, task_id: str):
    """按 task_instance_id（= TaskRequest.task_id）读 retry_task。

    retry 的 create_retry_task 把 failure.task_id 写到 task_instance_id 列
    （见 retry/consumer.py::_handle + repository.create_retry_task），所以这里
    用 task_id 反查。RLS 需 tenant 上下文。
    """
    async with conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", TENANT_ID)
        return await conn.fetchrow(
            """
            SELECT status, retry_count, max_attempts, last_error_code, trace_id
            FROM retry_task
            WHERE task_instance_id = $1
            ORDER BY id DESC
            LIMIT 1
            """,
            task_id,
        )


async def fetch_task_status(conn: asyncpg.Connection, task_id: str) -> str | None:
    """诊断用：读 task 表状态，确认 executor 真的跑了（pending→failed）。"""
    async with conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", TENANT_ID)
        return await conn.fetchval(
            "SELECT status FROM task WHERE id = $1",
            task_id,
        )


# ---------------------------------------------------------------------------
# Kafka 投递：只投 task-requests（永不投 task-failures）
# ---------------------------------------------------------------------------


async def produce_task_request(task_id: str, trace_id: str) -> None:
    """投一条 TaskRequest 到 task-requests。

    payload 字段对齐 apihub_core.events.TaskRequest（dataclass）：
      task_id / api_id / api_version_id / backend_url / payload / timeout_seconds
      + tenant_id / app_id / trace_id / request_id
    executor 消费侧 _handle 会用 header 覆盖 payload 里的 tenant_id/app_id/trace_id，
    所以这里【同时】塞 header（模拟 apihub_core.kafka.emit 的注入）和 payload（兜底）。
    """
    # asdict(TaskRequest) 的形状：TOPIC 是 ClassVar 不会进 dict
    msg = {
        "task_id": task_id,
        "api_id": API_ID,
        "api_version_id": API_VERSION_ID,
        "backend_url": DEAD_BACKEND_URL,
        "payload": json.dumps({"job": "r1a-smoke", "force": "fail"}),
        "timeout_seconds": 10.0,
        "callback_url": None,
        "tenant_id": TENANT_ID,
        "app_id": APP_ID,
        "request_id": f"req_{task_id}",
        "trace_id": trace_id,
    }
    headers = [
        ("tenant_id", TENANT_ID.encode("utf-8")),
        ("app_id", APP_ID.encode("utf-8")),
        ("trace_id", trace_id.encode("utf-8")),
    ]
    producer = aiokafka.AIOKafkaProducer(
        bootstrap_servers=KAFKA_BOOTSTRAP,
        value_serializer=lambda v: json.dumps(v).encode("utf-8"),
        key_serializer=lambda k: k.encode("utf-8") if k else None,
        acks="all",
        enable_idempotence=True,
    )
    await producer.start()
    try:
        # key=task_id：同任务进同一 partition（与 kafka.emit_event 行为一致）
        await producer.send_and_wait(TASK_TOPIC, msg, key=task_id, headers=headers)
    finally:
        await producer.stop()


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


async def main() -> int:
    task_id = f"task_r1a_smoke_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    trace_id = f"trace_r1a_{task_id}"
    print("== R1a retry-chain e2e runbook ==")
    print(f"  task_id      : {task_id}")
    print(f"  trace_id     : {trace_id}")
    print(f"  tenant       : {TENANT_ID} / app {APP_ID}")
    print(f"  backend_url  : {DEAD_BACKEND_URL}  (expect connect refused)")
    print(f"  kafka        : {KAFKA_BOOTSTRAP}")
    print(f"  pg           : {PG_DSN.split('@')[-1]}")
    print(
        "  注：本脚本【只】投 task-requests；task-failures 必须由 executor 自动产出，"
        "禁止手动注入（审计 §6）。"
    )

    conn = await asyncpg.connect(PG_DSN)
    try:
        # 1) 预置 pending task 行（executor mark_running 依赖）
        await insert_pending_task(conn, task_id, trace_id)
        print(f"\n[1/3] inserted pending task row: {task_id}")

        # 2) 投 TaskRequest（唯一的人工投递动作）
        await produce_task_request(task_id, trace_id)
        print(f"[2/3] produced TaskRequest to {TASK_TOPIC}: {task_id}")

        # 3) 轮询 retry_task 直到 dead + retry_count 耗尽，或超时
        print(
            f"[3/3] polling retry_task(task_instance_id={task_id}) until dead "
            f"(timeout {POLL_TIMEOUT_S:.0f}s, interval {POLL_INTERVAL_S}s)..."
        )
        deadline = time.time() + POLL_TIMEOUT_S
        last = None
        while time.time() < deadline:
            last = await fetch_retry_task(conn, task_id)
            if last is not None:
                status = last["status"]
                retry_count = int(last["retry_count"])
                max_attempts = int(last["max_attempts"])
                print(
                    f"      retry_task: status={status} "
                    f"retry_count={retry_count}/{max_attempts} "
                    f"err={last['last_error_code']}"
                )
                if status == "dead" and retry_count >= max_attempts:
                    # 链路全闭合：executor 产了 task-failures、retry 建了行、worker 跑完退避
                    task_status = await fetch_task_status(conn, task_id)
                    print("\n== PASS: retry-chain closed end-to-end ==")
                    print(f"  task.status         : {task_status} (expect failed)")
                    print("  retry_task.status   : dead")
                    print(f"  retry_count         : {retry_count} == max_attempts({max_attempts})")
                    print(f"  last_error_code     : {last['last_error_code']}")
                    print(f"  trace_id            : {last['trace_id']}")
                    print(
                        "  证明：executor 自动产 task-failures（无手动注入）→ retry 自动建 "
                        "retry_task → worker 耗尽退避到 dead。R1a §2.1/§2.2/§2.7 链路闭合。"
                    )
                    return 0
            await asyncio.sleep(POLL_INTERVAL_S)

        # 超时诊断：区分"retry_task 根本没出现"（断在 executor→task-failures）
        # vs "出现了但没到 dead"（断在 worker 退避）。
        print("\n== FAIL: timeout waiting for retry_task to reach dead ==")
        if last is None:
            task_status = await fetch_task_status(conn, task_id)
            print(f"  retry_task 未出现。task.status={task_status}。")
            if task_status in (None, "pending"):
                print("  → executor 可能没消费 task-requests（服务没起？）。")
            elif task_status == "running":
                print("  → executor 卡在 mark_running 之后、mark_failed 之前。")
            elif task_status in ("failed", "timeout"):
                print(
                    "  → executor 已 mark_failed 但【未】产 task-failures，或 retry "
                    "consumer 没消费。这正是 R1a §2.1 要堵的断链 —— 检查 executor 是否"
                    "带 R1a 代码、retry 服务是否在跑。"
                )
        else:
            print(
                f"  retry_task 出现但未到 dead: last={dict(last)}。"
                f" → retry worker 可能没在跑（make run-retry）。"
            )
        return 1
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
