"""deprecate / retire 生命周期端点测试。"""

import pytest


@pytest.fixture
def stub_db(monkeypatch):
    """覆盖 db.db_session，模拟 PG UPDATE。"""
    state = {
        "version_states": {},  # version_id → status
        "update_results": {},  # version_id → "UPDATE 1" / "UPDATE 0"
    }

    class _FakeConn:
        async def execute(self, sql, *args):
            # 简化：通过 args[0] = version_id 匹配 retire / deprecate 模式
            if args:
                vid = args[0]
                if (
                    "status = 'published'" in sql
                    and state["version_states"].get(vid) == "published"
                ):
                    state["version_states"][vid] = "deprecated"
                    return "UPDATE 1"
                if (
                    "status = 'deprecated'" in sql
                    and state["version_states"].get(vid) == "deprecated"
                ):
                    state["version_states"][vid] = "retired"
                    return "UPDATE 1"
                return "UPDATE 0"
            return "UPDATE 0"

        async def fetchrow(self, sql, *args):
            return None

        async def fetch(self, sql, *args):
            return []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    from apihub_core import db as db_mod

    @db_mod.asynccontextmanager
    async def _fake_session():
        yield _FakeConn()

    monkeypatch.setattr(db_mod, "db_session", _fake_session)
    return state


class TestDeprecate:
    async def test_deprecate_success(self, admin_client, stub_db, stub_kafka):
        """published → deprecated。"""
        stub_db["version_states"]["ver_123"] = "published"
        resp = await admin_client.post("/v1/api-versions/ver_123/deprecate")
        assert resp.status_code == 200
        assert resp.json()["status"] == "deprecated"

    async def test_deprecate_wrong_state_409(self, admin_client, stub_db, stub_kafka):
        """非 published → 409。"""
        stub_db["version_states"]["ver_x"] = "draft"
        resp = await admin_client.post("/v1/api-versions/ver_x/deprecate")
        assert resp.status_code == 409

    async def test_deprecate_not_found_409(self, admin_client, stub_db, stub_kafka):
        """version 不存在 → 409。"""
        resp = await admin_client.post("/v1/api-versions/ver_nonexistent/deprecate")
        assert resp.status_code == 409


class TestRetire:
    async def test_retire_after_deprecate(self, admin_client, stub_db, stub_kafka):
        """deprecated → retired。"""
        stub_db["version_states"]["ver_456"] = "deprecated"
        resp = await admin_client.post("/v1/api-versions/ver_456/retire")
        assert resp.status_code == 200
        assert resp.json()["status"] == "retired"

    async def test_retire_directly_from_published_409(self, admin_client, stub_db, stub_kafka):
        """published → retired 必须先 deprecated（避免误下线）。"""
        stub_db["version_states"]["ver_skip"] = "published"
        resp = await admin_client.post("/v1/api-versions/ver_skip/retire")
        assert resp.status_code == 409
