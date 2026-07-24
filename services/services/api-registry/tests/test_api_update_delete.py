"""A3: PATCH/DELETE /v1/apis/{id} —— stub_db 假 conn，覆盖护栏与幂等。"""

from contextlib import asynccontextmanager

import pytest


@pytest.fixture
def stub_api_db(monkeypatch):
    """假 conn：跟踪 apis(id→row) + versions([{api_id,status}])，匹配 PATCH/DELETE 的 SQL 模式。"""
    apis: dict[str, dict] = {}
    versions: list[dict] = []

    class _Conn:
        async def fetchval(self, sql, *args):
            if "EXISTS" in sql and "api_version" in sql:
                api_id = args[0]
                return any(
                    v["api_id"] == api_id
                    and v["status"] in ("published", "deprecated", "reviewing")
                    for v in versions
                )
            return None

        async def fetchrow(self, sql, *args):
            if sql.startswith("UPDATE api SET"):
                api_id = args[0]
                row = apis.get(api_id)
                if row is None:
                    return None
                set_part = sql.split("SET ", 1)[1].split(", updated_at", 1)[0]
                for pair in set_part.split(","):
                    col, _, idx = pair.strip().partition(" = ")
                    n = int(idx.replace("$", ""))
                    row[col] = args[n - 1]
                return dict(row)
            if sql.startswith("SELECT * FROM api WHERE id"):
                return dict(apis.get(args[0])) if args[0] in apis else None
            return None

        async def execute(self, sql, *args):
            if "DELETE FROM api_version" in sql:
                api_id = args[0]
                before = len(versions)
                versions[:] = [v for v in versions if v["api_id"] != api_id]
                return f"DELETE {before - len(versions)}"
            if "DELETE FROM api" in sql:
                return "DELETE 1" if args[0] in apis else "DELETE 0"
            return "UPDATE 1"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    @asynccontextmanager
    async def _fake_session():
        yield _Conn()

    from apihub_core import db as db_mod

    monkeypatch.setattr(db_mod, "db_session", _fake_session)

    def _seed(api_id, **fields):
        apis[api_id] = {
            "id": api_id,
            "tenant_id": "42",
            "name": "n",
            "description": None,
            "category": "c",
            "base_path": "/x",
            "tags": [],
            "status": "draft",
            "visibility": "private",
            **fields,
        }

    return {
        "seed": _seed,
        "add_version": lambda api_id, status: versions.append({"api_id": api_id, "status": status}),
    }


pytestmark = pytest.mark.asyncio


async def test_patch_updates_fields(admin_client, stub_api_db, stub_kafka):
    stub_api_db["seed"]("api_1", name="old", description="d", category="c", tags=["a"])
    r = await admin_client.patch("/v1/apis/api_1", json={"name": "new", "tags": ["x", "y"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "new"
    assert body["tags"] == ["x", "y"]
    assert any(p[1]["action"] == "api.update" for p in stub_kafka)


async def test_patch_rejects_base_path(admin_client, stub_api_db):
    stub_api_db["seed"]("api_1")
    r = await admin_client.patch("/v1/apis/api_1", json={"base_path": "/changed"})
    assert r.status_code == 422


async def test_patch_unknown_api_404(admin_client, stub_api_db):
    r = await admin_client.patch("/v1/apis/nope", json={"name": "xy"})
    assert r.status_code == 404


async def test_patch_empty_body_is_idempotent(admin_client, stub_api_db):
    stub_api_db["seed"]("api_1", name="keep")
    r = await admin_client.patch("/v1/apis/api_1", json={})
    assert r.status_code == 200
    assert r.json()["name"] == "keep"


async def test_delete_blocked_when_published_version(admin_client, stub_api_db):
    stub_api_db["seed"]("api_1")
    stub_api_db["add_version"]("api_1", "published")
    r = await admin_client.delete("/v1/apis/api_1")
    assert r.status_code == 409


async def test_delete_cascades_when_only_draft_retired(admin_client, stub_api_db):
    stub_api_db["seed"]("api_1")
    stub_api_db["add_version"]("api_1", "draft")
    stub_api_db["add_version"]("api_1", "retired")
    r = await admin_client.delete("/v1/apis/api_1")
    assert r.status_code == 200, r.text
    assert r.json() == {"id": "api_1", "status": "deleted"}


async def test_delete_unknown_api_404(admin_client, stub_api_db):
    r = await admin_client.delete("/v1/apis/nope")
    assert r.status_code == 404
