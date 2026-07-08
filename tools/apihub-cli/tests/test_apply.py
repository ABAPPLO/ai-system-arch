"""apply 流程测试 —— stub httpx，验证 CLI → api-registry 的调用顺序与 payload。"""

import pytest
import respx
from apihub_cli.apply import apply_definition
from apihub_cli.client import RegistryClient
from apihub_cli.models import load_yaml


@pytest.fixture
def client():
    return RegistryClient(base_url="http://test", api_key="ak_test")


@pytest.fixture
def defn(sample_yaml_file):
    return load_yaml(sample_yaml_file)


class TestCreateFlow:
    """新 API（api-registry 没有同名记录）→ 创建 + 提工单。"""

    def test_creates_api_when_missing(self, client, defn):
        with respx.mock(base_url="http://test") as mock:
            # find_api_by_name 走 list_apis 过滤
            mock.get("/v1/apis").respond(
                200, json={"items": [], "limit": 200, "offset": 0},
            )
            mock.post("/v1/apis").respond(200, json={"api_id": "api_001"})
            mock.post("/v1/api-versions").respond(
                200,
                json={
                    "id": "ver_001",
                    "api_id": "api_001",
                    "version": "v1",
                    "backend_type": "http",
                    "backend_url": "http://x",
                    "status": "draft",
                    "created_at": "2026-07-07T00:00:00Z",
                },
            )
            mock.post("/v1/change-requests").respond(
                201, json={"request_id": 42, "status": "approved"},
            )

            r = apply_definition(
                defn, client=client,
                target_env="dev", submitted_by="ci@apihub",
            )

        assert r.is_ok()
        assert r.api_id == "api_001"
        assert r.version_id == "ver_001"
        assert r.change_request_id == 42
        assert r.change_request_status == "approved"
        assert r.error is None

    def test_create_api_payload_includes_owner_excluded(
        self, client, defn,
    ):
        """owner 字段不入库（PG api 表没这列），不应出现在 POST /v1/apis 的 payload。"""
        with respx.mock(base_url="http://test") as mock:
            mock.get("/v1/apis").respond(200, json={"items": []})
            create_api_route = mock.post("/v1/apis").respond(
                200, json={"api_id": "api_002"},
            )
            mock.post("/v1/api-versions").respond(
                200,
                json={
                    "id": "ver_002", "api_id": "api_002", "version": "v1",
                    "backend_type": "http", "backend_url": "x",
                    "status": "draft", "created_at": "2026-07-07T00:00:00Z",
                },
            )
            mock.post("/v1/change-requests").respond(
                201, json={"request_id": 1, "status": "approved"},
            )

            apply_definition(
                defn, client=client,
                target_env="dev", submitted_by="u_alice",
            )

        sent = create_api_route.calls[0].request.read()
        import json
        body = json.loads(sent)
        assert "owner" not in body
        assert body["name"] == "user-query"
        assert body["base_path"] == "/user-service"


class TestExistingApi:
    """API 已存在 → 跳过创建，复用 api_id。"""

    def test_skips_create_when_exists(self, client, defn):
        with respx.mock(base_url="http://test", assert_all_called=False) as mock:
            mock.get("/v1/apis").respond(
                200,
                json={
                    "items": [
                        {
                            "id": "api_existing",
                            "name": "user-query",
                            "base_path": "/user-service",
                        }
                    ],
                    "limit": 200, "offset": 0,
                },
            )
            create_api = mock.post("/v1/apis").respond(
                200, json={"api_id": "should_not_be_called"},
            )
            mock.post("/v1/api-versions").respond(
                200,
                json={
                    "id": "ver_new", "api_id": "api_existing", "version": "v1",
                    "backend_type": "http", "backend_url": "x",
                    "status": "draft", "created_at": "2026-07-07T00:00:00Z",
                },
            )
            cr_route = mock.post("/v1/change-requests").respond(
                201, json={"request_id": 7, "status": "pending"},
            )

            r = apply_definition(
                defn, client=client,
                target_env="prod", submitted_by="u_alice",
            )

        assert r.is_ok()
        assert r.api_id == "api_existing"
        assert r.change_request_status == "pending"
        # POST /v1/apis 不应被调
        assert create_api.calls.call_count == 0
        # change_request.change_type=update（API 已存在）
        import json
        cr_body = json.loads(cr_route.calls[0].request.read())
        assert cr_body["change_type"] == "update"
        assert cr_body["target_env"] == "prod"


class TestErrorHandling:
    def test_registry_error_returns_failure(self, client, defn):
        with respx.mock(base_url="http://test") as mock:
            mock.get("/v1/apis").respond(
                500, json={"code": 1, "message": "db down"},
            )
            r = apply_definition(
                defn, client=client,
                target_env="dev", submitted_by="ci",
            )

        assert not r.is_ok()
        assert r.error is not None
        assert "500" in r.error

    def test_change_request_4xx_records_error(self, client, defn):
        with respx.mock(base_url="http://test") as mock:
            mock.get("/v1/apis").respond(200, json={"items": []})
            mock.post("/v1/apis").respond(200, json={"api_id": "api_x"})
            mock.post("/v1/api-versions").respond(
                200,
                json={
                    "id": "ver_x", "api_id": "api_x", "version": "v1",
                    "backend_type": "http", "backend_url": "x",
                    "status": "draft", "created_at": "2026-07-07T00:00:00Z",
                },
            )
            mock.post("/v1/change-requests").respond(
                422, json={"code": 40003, "message": "invalid env"},
            )

            r = apply_definition(
                defn, client=client,
                target_env="bad-env", submitted_by="ci",
            )

        assert not r.is_ok()
        assert "422" in r.error


class TestAIModelPayload:
    def test_ai_fields_passed_to_version(self, client, ai_yaml_file):
        defn = load_yaml(ai_yaml_file)
        with respx.mock(base_url="http://test") as mock:
            mock.get("/v1/apis").respond(200, json={"items": []})
            mock.post("/v1/apis").respond(200, json={"api_id": "api_ai"})
            ver_route = mock.post("/v1/api-versions").respond(
                200,
                json={
                    "id": "ver_ai", "api_id": "api_ai", "version": "v1",
                    "backend_type": "ai_model", "backend_url": "x",
                    "status": "draft", "created_at": "2026-07-07T00:00:00Z",
                },
            )
            mock.post("/v1/change-requests").respond(
                201, json={"request_id": 99, "status": "approved"},
            )

            apply_definition(
                defn, client=client,
                target_env="dev", submitted_by="ci",
            )

        import json
        body = json.loads(ver_route.calls[0].request.read())
        assert body["backend_type"] == "ai_model"
        assert body["ai_model"] == "qwen-max"
        assert body["ai_streaming"] is True
