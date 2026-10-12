"""security 校验测试 —— namespace 白名单 + spec 危险字段拒绝。"""

import pytest
from apihub_core.errors import ApiError
from workflow_svc.security import validate_namespace, validate_spec_safety


@pytest.fixture(autouse=True)
def _clear_settings():
    from apihub_core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class TestValidateNamespace:
    def test_default_namespace_allowed(self):
        validate_namespace("apihub-workflow")  # 默认白名单，不抛即过

    def test_not_in_allowlist_rejected(self):
        with pytest.raises(ApiError) as e:
            validate_namespace("kube-system")
        assert e.value.http_status == 403

    def test_custom_allowlist_via_env(self, monkeypatch):
        monkeypatch.setenv("ARGO_ALLOWED_NAMESPACES", "ns-a, ns-b")
        from apihub_core.config import get_settings

        get_settings.cache_clear()
        validate_namespace("ns-a")
        validate_namespace("ns-b")
        with pytest.raises(ApiError):
            validate_namespace("apihub-workflow")

    def test_invalid_namespace_format_rejected(self):
        with pytest.raises(ApiError):
            validate_namespace("Bad_NS")
        with pytest.raises(ApiError):
            validate_namespace("")

    def test_path_tricks_rejected(self):
        with pytest.raises(ApiError):
            validate_namespace("apihub-workflow;evil")
        with pytest.raises(ApiError):
            validate_namespace("kube-system")


class TestValidateSpecSafety:
    def test_plain_spec_ok(self):
        validate_spec_safety(
            {
                "entrypoint": "main",
                "templates": [{"name": "main", "script": {"image": "python:3.11"}}],
            }
        )

    def test_privileged_rejected_at_any_depth(self):
        with pytest.raises(ApiError) as e:
            validate_spec_safety(
                {
                    "templates": [
                        {
                            "name": "main",
                            "container": {
                                "image": "x",
                                "securityContext": {"privileged": True},
                            },
                        }
                    ]
                }
            )
        assert e.value.http_status == 403

    def test_privileged_false_ok(self):
        validate_spec_safety(
            {"templates": [{"name": "m", "container": {"securityContext": {"privileged": False}}}]}
        )

    @pytest.mark.parametrize("key", ["hostNetwork", "hostPID", "hostIPC"])
    def test_host_flags_rejected(self, key):
        with pytest.raises(ApiError):
            validate_spec_safety({"templates": [{"name": "m", key: True}]})

    def test_hostpath_volume_rejected(self):
        with pytest.raises(ApiError):
            validate_spec_safety(
                {
                    "templates": [
                        {
                            "name": "m",
                            "container": {"image": "x"},
                            "volumes": [{"name": "host", "hostPath": {"path": "/"}}],
                        }
                    ]
                }
            )

    def test_nested_in_arguments_rejected(self):
        """藏在 arguments/artifacts 等深层嵌套里也不行（递归 walk）。"""
        with pytest.raises(ApiError):
            validate_spec_safety(
                {
                    "arguments": {
                        "parameters": [
                            {"name": "p", "value": {"hostPID": True}},
                        ]
                    }
                }
            )

    def test_non_dict_rejected(self):
        with pytest.raises(ApiError):
            validate_spec_safety(["not", "a", "dict"])  # type: ignore[arg-type]


class TestSubmitEndpointGuard:
    """HTTP 层：危险 spec / 越界 namespace 在进 Argo 前就被拒。"""

    async def test_submit_privileged_spec_403(self, client, stub_repo):
        resp = await client.post(
            "/v1/workflows",
            json={
                "api_id": "1",
                "app_id": "1",
                "trace_id": "tr",
                "spec": {
                    "templates": [
                        {
                            "name": "m",
                            "container": {
                                "image": "x",
                                "securityContext": {"privileged": True},
                            },
                        }
                    ]
                },
            },
        )
        assert resp.status_code == 403

    async def test_submit_bad_namespace_403(self, client, stub_repo):
        resp = await client.post(
            "/v1/workflows",
            json={
                "api_id": "1",
                "app_id": "1",
                "trace_id": "tr",
                "namespace": "kube-system",
                "spec": {"entrypoint": "main"},
            },
        )
        assert resp.status_code == 403
