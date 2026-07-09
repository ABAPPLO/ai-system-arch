"""event payload 构造测试 —— 验证调用事件字段齐全。"""

from apihub_core.tenant import set_tenant_context
from dispatcher.event import build_call_event, new_request_id


class TestBuildCallEvent:
    def test_minimum_fields(self, tenant_a):
        set_tenant_context(tenant_a)
        event = build_call_event(
            api_id="api_1",
            api_version_id="ver_1",
            method="GET",
            path="/v1/foo",
            status_code=200,
            is_success=True,
            latency_ms=42,
            request_size=100,
            response_size=500,
        )

        # 必填字段
        assert event["tenant_id"] == "tenant_a"
        assert event["tenant_type"] == "internal"
        assert event["app_id"] == "app_trading"
        assert event["api_id"] == "api_1"
        assert event["api_version_id"] == "ver_1"
        assert event["method"] == "GET"
        assert event["path"] == "/v1/foo"
        assert event["status_code"] == 200
        assert event["is_success"] == 1
        assert event["latency_ms"] == 42
        assert event["request_size"] == 100
        assert event["response_size"] == 500
        assert event["backend_type"] == "http"

        # 自动生成
        assert event["trace_id"].startswith("trc_")
        assert event["request_id"].startswith("req_")

    def test_is_success_serialized_as_int(self, tenant_a):
        set_tenant_context(tenant_a)
        event = build_call_event(
            api_id="a",
            api_version_id="v",
            method="GET",
            path="/",
            status_code=500,
            is_success=False,
            latency_ms=10,
            request_size=0,
            response_size=0,
        )
        assert event["is_success"] == 0
        assert isinstance(event["is_success"], int)

    def test_ai_streaming_serialized_as_int(self, tenant_a):
        set_tenant_context(tenant_a)
        event = build_call_event(
            api_id="a",
            api_version_id="v",
            method="POST",
            path="/llm",
            status_code=200,
            is_success=True,
            latency_ms=1500,
            request_size=50,
            response_size=2000,
            backend_type="ai_model",
            ai_model="gpt-4o-mini",
            ai_streaming=True,
            token_prompt=120,
            token_completion=80,
            token_total=200,
        )
        assert event["backend_type"] == "ai_model"
        assert event["ai_model"] == "gpt-4o-mini"
        assert event["ai_streaming"] == 1
        assert event["token_prompt"] == 120
        assert event["token_completion"] == 80
        assert event["token_total"] == 200

    def test_no_tenant_context_empty_strings(self):
        """无租户上下文（不应该发生在 dispatcher 里，但要安全降级）。"""
        event = build_call_event(
            api_id="a",
            api_version_id="v",
            method="GET",
            path="/",
            status_code=200,
            is_success=True,
            latency_ms=10,
            request_size=0,
            response_size=0,
        )
        assert event["tenant_id"] == ""
        assert event["tenant_type"] == ""
        assert event["app_id"] == ""

    def test_method_uppercased(self, tenant_a):
        set_tenant_context(tenant_a)
        event = build_call_event(
            api_id="a",
            api_version_id="v",
            method="post",
            path="/",
            status_code=200,
            is_success=True,
            latency_ms=10,
            request_size=0,
            response_size=0,
        )
        assert event["method"] == "POST"

    def test_explicit_trace_id_preserved(self, tenant_a):
        set_tenant_context(tenant_a)
        event = build_call_event(
            api_id="a",
            api_version_id="v",
            method="GET",
            path="/",
            status_code=200,
            is_success=True,
            latency_ms=10,
            request_size=0,
            response_size=0,
            trace_id="trc_provided_123",
            request_id="req_provided_456",
        )
        assert event["trace_id"] == "trc_provided_123"
        assert event["request_id"] == "req_provided_456"


class TestNewRequestId:
    def test_format(self):
        rid = new_request_id()
        assert rid.startswith("req_")
        assert len(rid) > 10

    def test_unique(self):
        ids = {new_request_id() for _ in range(100)}
        assert len(ids) == 100
