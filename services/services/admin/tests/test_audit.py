"""audit helper 单测 —— 验证 path → action 的推断逻辑。"""


class FakeUrl:
    def __init__(self, path):
        self.path = path


class FakeClient:
    def __init__(self, host="10.0.0.1"):
        self.host = host


class FakeHeaders:
    def __init__(self, h):
        self._h = h

    def get(self, k, default=None):
        return self._h.get(k, default)


class FakeRequest:
    """构造最小可用的 Request 对象供 audit helper 用。"""

    def __init__(self, method, path, headers=None):
        self.method = method
        self.url = FakeUrl(path)
        self.client = FakeClient()
        self.headers = FakeHeaders(headers or {})


from admin.audit import _infer_action  # noqa: E402


class TestInferAction:
    def test_post_admin_tenants(self):
        req = FakeRequest("POST", "/v1/admin/tenants")
        action, rt, rid = _infer_action(req)
        assert action == "create_tenants"
        assert rt == "tenants"

    def test_put_admin_tenant_by_id(self):
        req = FakeRequest("PUT", "/v1/admin/tenants/t1")
        action, rt, rid = _infer_action(req)
        assert action == "update_tenants"
        assert rt == "tenants"
        assert rid == "t1"

    def test_post_suspend_verb(self):
        req = FakeRequest("POST", "/v1/admin/tenants/t1/suspend")
        action, rt, rid = _infer_action(req)
        assert action == "suspend_tenants"
        assert rt == "tenants"
        assert rid == "t1"

    def test_post_resume_verb(self):
        req = FakeRequest("POST", "/v1/admin/tenants/t1/resume")
        action, _, _ = _infer_action(req)
        assert action == "resume_tenants"

    def test_delete_member(self):
        req = FakeRequest("DELETE", "/v1/admin/tenants/t1/members/u2")
        action, rt, rid = _infer_action(req)
        # DELETE + 末段是 id（u2）→ delete + 父资源（members）
        assert action == "delete_members"

    def test_get_not_audited(self):
        """GET 不审计。"""
        req = FakeRequest("GET", "/v1/admin/tenants")
        action, _, _ = _infer_action(req)
        assert action == ""

    def test_health_skipped(self):
        req = FakeRequest("POST", "/health")
        action, _, _ = _infer_action(req)
        assert action == ""

    def test_metrics_skipped(self):
        req = FakeRequest("POST", "/metrics")
        action, _, _ = _infer_action(req)
        assert action == ""

    def test_delete_apikey(self):
        req = FakeRequest("DELETE", "/v1/api-keys/key_xxx")
        action, rt, rid = _infer_action(req)
        assert action == "delete_api-keys"
        assert rid == "key_xxx"
