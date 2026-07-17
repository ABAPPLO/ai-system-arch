"""deprecate / retire 生命周期端点测试。"""

import pytest


@pytest.fixture
def stub_db(monkeypatch):
    """覆盖 db.db_session，模拟 PG UPDATE + publish 的 fetchrow。"""
    state = {
        "version_states": {},  # version_id → status
        "update_results": {},  # version_id → "UPDATE 1" / "UPDATE 0"
        "rows": {},  # version_id → {method, path, base_path} 供 publish fetchrow 返回
    }

    class _FakeConn:
        async def execute(self, sql, *args):
            # 简化：通过 args[0] = version_id 匹配 publish/deprecate/retire 模式
            if args:
                vid = args[0]
                cur = state["version_states"].get(vid)
                # publish: draft/reviewing → published（SET status='published'）
                if "SET status = 'published'" in sql and cur in ("draft", "reviewing"):
                    state["version_states"][vid] = "published"
                    return "UPDATE 1"
                # deprecate: published → deprecated（WHERE status='published'）
                if "status = 'published'" in sql and cur == "published":
                    state["version_states"][vid] = "deprecated"
                    return "UPDATE 1"
                # retire: deprecated → retired（WHERE status='deprecated'）
                if "status = 'deprecated'" in sql and cur == "deprecated":
                    state["version_states"][vid] = "retired"
                    return "UPDATE 1"
                return "UPDATE 0"
            return "UPDATE 0"

        async def fetchrow(self, sql, *args):
            # publish handler:
            #   SELECT v.*, a.base_path FROM api_version v JOIN api a ON a.id=v.api_id
            #   WHERE v.id=$1 AND v.status IN ('draft','reviewing')
            if args:
                vid = args[0]
                cur = state["version_states"].get(vid)
                if cur in ("draft", "reviewing"):
                    overrides = state["rows"].get(vid, {})
                    return {
                        "id": vid,
                        "method": overrides.get("method", "GET"),
                        "path": overrides.get("path", "/test"),
                        "base_path": overrides.get("base_path", "/api/test"),
                        "status": cur,
                    }
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


class TestPublish:
    async def test_publish_calls_apisix_before_status(
        self, admin_client, stub_db, stub_kafka, monkeypatch
    ):
        """publish 先下发 APISIX 路由，成功才置 published。"""
        stub_db["version_states"]["ver_pub"] = "draft"
        stub_db["rows"]["ver_pub"] = {
            "method": "POST",
            "path": "/orders",
            "base_path": "/shop",
        }

        captured: dict = {}

        async def _fake_publish(*, version_id, method, path, base_path):
            # publish_route 必须在 status UPDATE 之前调用 —— 此刻状态应仍为 draft
            captured["state_at_call"] = stub_db["version_states"].get(version_id)
            captured.update(
                version_id=version_id,
                method=method,
                path=path,
                base_path=base_path,
            )

        from apihub_core import apisix_client

        monkeypatch.setattr(apisix_client, "publish_route", _fake_publish)

        resp = await admin_client.post("/v1/api-versions/ver_pub/publish")

        assert resp.status_code == 200
        assert resp.json()["status"] == "published"
        # publish_route 被调用，且参数来自 api_version 行 + api.base_path
        assert captured["version_id"] == "ver_pub"
        assert captured["method"] == "POST"
        assert captured["path"] == "/orders"
        assert captured["base_path"] == "/shop"
        # 关键顺序断言：publish_route 在 status UPDATE 之前被调用（状态仍为 draft）
        assert captured["state_at_call"] == "draft"
        # UPDATE 已生效
        assert stub_db["version_states"]["ver_pub"] == "published"
