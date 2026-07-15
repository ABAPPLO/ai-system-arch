"""repository.create_retry_task 幂等集成测试 —— 需 dev PG（TEST_PG_DSN）。

跑法（从 services/services/retry 下）::

    TEST_PG_DSN="postgresql://apihub_app:apihub_app_dev_pwd@localhost:15443/apihub" \
      /home/applo/project/ai-system-arch/.venv/bin/python -m pytest tests/test_repository.py -q

没有 TEST_PG_DSN 时整体 skip（不打无 PG 的空跑）。
"""

import os
import urllib.parse

import pytest
from apihub_core import db
from apihub_core.config import Settings


def _parse_dsn(dsn: str) -> dict:
    """把 postgresql://user:pwd@host:port/db 解析成 Settings 的 pg_* 字段。"""
    p = urllib.parse.urlparse(dsn)
    return {
        "pg_host": p.hostname or "localhost",
        "pg_port": str(p.port or 5432),
        "pg_database": (p.path or "/apihub").lstrip("/"),
        "pg_user": p.username or "apihub",
        "pg_password": p.password or "",
    }


@pytest.fixture
async def pg_pool():
    """初始化 apihub_core.db 的进程级 pool（create_retry_task 走 admin_db_session）。

    teardown 时关闭 pool，避免污染其它测试模块的 _pool 全局。
    """
    dsn = os.environ.get("TEST_PG_DSN")
    if not dsn:
        pytest.skip("TEST_PG_DSN not set; needs dev PG stack")
    settings = Settings(**_parse_dsn(dsn))
    await db.init_pool(settings)
    try:
        yield
    finally:
        await db.close_pool()


class TestCreateRetryTaskIdempotency:
    async def test_create_retry_task_dedups_active(self, pg_pool):
        """同 task_instance_id 已有活跃 retry_task 时，再次插入返回 0（去重），不报错。

        依赖 partial unique index idx_retry_task_active_dedup（10-r1a-retry-idempotency.sql）
        + repository 的 ON CONFLICT DO NOTHING。
        """
        from datetime import UTC, datetime, timedelta

        from retry_svc import repository as repo
        from retry_svc.models import BackoffPolicy

        nxt = datetime.now(UTC) + timedelta(seconds=1)
        common = {
            "tenant_id": "t1",
            "trace_id": "trc_1",
            "api_id": "api1",
            "app_id": "a1",
            "task_instance_id": "task_dup",
            "original_request": {},
            "error_code": "x",
            "error_msg": "y",
            "max_attempts": 3,
            "backoff_policy": BackoffPolicy.EXPONENTIAL,
            "backoff_base_ms": 1000,
            "next_retry_at": nxt,
            "env": "dev",
        }
        try:
            rid1 = await repo.create_retry_task(**common)
            assert rid1 > 0
            rid2 = await repo.create_retry_task(**common)  # 同 task_instance_id="task_dup"
            assert rid2 == 0  # 去重：partial unique 命中 → ON CONFLICT DO NOTHING → 返回 0
        finally:
            # 清理：删 task_dup 行（retry_attempt 经 ON DELETE CASCADE 跟着删），避免污染 dev 库
            async with db.admin_db_session() as conn:
                await conn.execute(
                    "DELETE FROM retry_task WHERE task_instance_id = $1",
                    "task_dup",
                )
