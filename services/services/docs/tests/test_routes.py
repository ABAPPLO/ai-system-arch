"""HTTP 端点测试 —— httpx ASGITransport 直打 app。"""

from datetime import datetime

from docs import repository as repo_mod


class _FakeRow(dict):
    """asyncpg Record 风格 dict —— 既支持 dict 访问，又支持 row['key']。"""


class TestGetOpenapiJson:
    async def test_returns_spec(self, client, monkeypatch):
        async def _fake_meta(api_id, version=None):
            from docs.models import ApiMeta

            return ApiMeta(
                api_id=api_id,
                api_name="Echo",
                base_path="/echo",
                version="v1",
                version_id="ver_x",
                backend_type="http",
                backend_url="http://x",
                category="utility",
                api_status="published",
                version_status="published",
            )

        monkeypatch.setattr(repo_mod, "get_api_meta", _fake_meta)

        resp = await client.get("/v1/docs/apis/api_x/openapi.json")
        assert resp.status_code == 200
        spec = resp.json()
        assert spec["openapi"] == "3.0.3"
        assert spec["info"]["title"] == "Echo"
        assert "/echo" in spec["paths"]

    async def test_not_found(self, client, monkeypatch):
        from apihub_core.errors import ApiError, ErrorCode

        async def _raise(api_id, version=None):
            raise ApiError(ErrorCode.NOT_FOUND, "not found")

        monkeypatch.setattr(repo_mod, "get_api_meta", _raise)

        resp = await client.get("/v1/docs/apis/missing/openapi.json")
        assert resp.status_code == 404


class TestGetOpenapiYaml:
    async def test_returns_yaml(self, client, monkeypatch):
        async def _fake_meta(api_id, version=None):
            from docs.models import ApiMeta

            return ApiMeta(
                api_id=api_id,
                api_name="Echo",
                base_path="/echo",
                version="v1",
                version_id="ver_x",
                backend_type="http",
                backend_url="http://x",
                category="utility",
                api_status="published",
                version_status="published",
            )

        monkeypatch.setattr(repo_mod, "get_api_meta", _fake_meta)

        resp = await client.get("/v1/docs/apis/api_x/openapi.yaml")
        assert resp.status_code == 200
        assert "openapi: '3.0.3'" in resp.text or "openapi: 3.0.3" in resp.text


class TestGetExamples:
    async def test_examples(self, client, monkeypatch):
        async def _fake_meta(api_id, version=None):
            from docs.models import ApiMeta

            return ApiMeta(
                api_id=api_id,
                api_name="Echo",
                base_path="/echo",
                version="v1",
                version_id="ver_x",
                backend_type="http",
                backend_url="http://x",
                category="utility",
                api_status="published",
                version_status="published",
            )

        monkeypatch.setattr(repo_mod, "get_api_meta", _fake_meta)

        resp = await client.get("/v1/docs/apis/api_x/examples")
        assert resp.status_code == 200
        body = resp.json()
        assert "curl" in body
        assert "python" in body
        assert "javascript" in body
        assert isinstance(body["notes"], list)


class TestListVersions:
    async def test_list_versions(self, client, monkeypatch):
        async def _list(api_id):
            return [
                {
                    "id": "ver_1",
                    "version": "v1",
                    "status": "deprecated",
                    "backend_type": "http",
                    "created_at": datetime(2026, 6, 1),
                    "published_at": datetime(2026, 6, 2),
                },
                {
                    "id": "ver_2",
                    "version": "v2",
                    "status": "published",
                    "backend_type": "http",
                    "created_at": datetime(2026, 7, 1),
                    "published_at": datetime(2026, 7, 2),
                },
            ]

        monkeypatch.setattr(repo_mod, "list_versions", _list)

        resp = await client.get("/v1/docs/apis/api_x/versions")
        assert resp.status_code == 200
        body = resp.json()
        assert body["api_id"] == "api_x"
        assert len(body["items"]) == 2
        assert body["items"][0]["version"] == "v1"


class TestHealth:
    async def test_health(self, client):
        resp = await client.get("/v1/docs/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "service": "docs"}
