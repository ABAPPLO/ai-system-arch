"""K8sArgoClient 单测 —— 用 httpx.MockTransport 打桩 K8s API server。

覆盖 Task 6 e2e 修过的两个真 bug（StubArgoClient 测不到、e2e 只 kind-only）：
  - get_status：manual-stop（phase=Failed + message="Stopped with strategy 'Stop'"）
    映射回 CANCELLED，区别于真失败。
  - stream_logs：按 Argo node-name annotation 过滤 step pod（旧代码按 pod 名子串
    过滤，对真 node 名恒为空 → 误导性 "no pods found"）。
"""

import httpx
import pytest
from workflow_svc.argo_client import K8sArgoClient
from workflow_svc.models import WorkflowStatus

# K8sArgoClient.__init__ 会建一个 httpx.AsyncClient（随即被 MockTransport 替换），
# 但 httpx 构造时会读 *_PROXY 环境变量；本机 ALL_PROXY=socks://... 的 scheme
# 被 httpx 拒收。测试全程不触网，清掉代理即可。
_PROXY_VARS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
)


@pytest.fixture(autouse=True)
def _no_proxy_env(monkeypatch):
    for v in _PROXY_VARS:
        monkeypatch.delenv(v, raising=False)
    yield


def _make_client(handler, server_handler=None):
    """构造一个 K8sArgoClient，绕过 in-cluster token 读取 + 真 TLS。

    - 显式传 token 跳过 _read_sa_token。
    - 传真实 ca_cert_path（certifi）让 __init__ 的 CRD client 能构造。
    - MockTransport 替换 _client（CRD）+ 可选 _server_client（argo-server）。
    """
    import certifi

    c = K8sArgoClient(token="fake-token", ca_cert_path=certifi.where())  # noqa: S106
    c._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://k8s.test",
        headers={"Authorization": "Bearer fake-token", "Accept": "application/json"},
    )
    if server_handler is not None:
        c._server_client = httpx.AsyncClient(
            transport=httpx.MockTransport(server_handler),
            base_url="https://argo-server.argo:2746",
            headers={"Authorization": "Bearer fake-token", "Accept": "application/json"},
        )
    return c


def _wf_status_payload(phase, message=None):
    """拼一个 Argo Workflow CRD 的 status 子树。"""
    status = {"phase": phase}
    if message is not None:
        status["message"] = message
    return {"status": status}


# ============ get_status ============


class TestGetStatusStopStrategy:
    """manual-stop 与真失败的区分 —— e2e 修过的 cancel 映射。"""

    async def test_failed_with_stop_strategy_maps_to_cancelled(self):
        """phase=Failed + message="Stopped with strategy 'Stop'" → CANCELLED。"""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=_wf_status_payload("Failed", "Stopped with strategy 'Stop'"),
            )

        c = _make_client(handler)
        try:
            status, msg = await c.get_status(namespace="apihub-workflow", argo_name="wf-x")
            assert status is WorkflowStatus.CANCELLED
            assert msg == "Stopped with strategy 'Stop'"
        finally:
            await c.close()

    async def test_failed_with_real_error_stays_failed(self):
        """phase=Failed + 真错误 message 必须保持 FAILED（不过度匹配）。"""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=_wf_status_payload("Failed", "some real error"),
            )

        c = _make_client(handler)
        try:
            status, _msg = await c.get_status(namespace="apihub-workflow", argo_name="wf-x")
            assert status is WorkflowStatus.FAILED
        finally:
            await c.close()

    async def test_succeeded_maps_to_succeeded(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_wf_status_payload("Succeeded"))

        c = _make_client(handler)
        try:
            status, msg = await c.get_status(namespace="apihub-workflow", argo_name="wf-x")
            assert status is WorkflowStatus.SUCCEEDED
            assert msg is None
        finally:
            await c.close()

    async def test_stop_message_must_start_with_phrase(self):
        """锚定：stop 短语在中间不应误判为 cancel（startswith 而非 in）。"""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=_wf_status_payload("Failed", "error: something Stopped with strategy foo"),
            )

        c = _make_client(handler)
        try:
            status, _msg = await c.get_status(namespace="apihub-workflow", argo_name="wf-x")
            # 短语不在开头 → 真失败
            assert status is WorkflowStatus.FAILED
        finally:
            await c.close()

    async def test_not_found_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"kind": "Status", "message": "nf"})

        from workflow_svc.argo_client import ArgoError

        c = _make_client(handler)
        try:
            with pytest.raises(ArgoError, match="not found"):
                await c.get_status(namespace="apihub-workflow", argo_name="nope")
        finally:
            await c.close()


# ============ stream_logs ============


def _pod(name, node_name=None, annotations_extra=None):
    """拼一个 core v1 Pod 对象（仅 metadata 部分）。"""
    ann = {}
    if node_name is not None:
        ann["workflows.argoproj.io/node-name"] = node_name
    if annotations_extra:
        ann.update(annotations_extra)
    meta = {"name": name}
    if ann:
        meta["annotations"] = ann
    return {"metadata": meta}


class TestStreamLogs:
    async def test_all_pods_when_no_step_name(self):
        """step_name=None → 拉全部 step pod 的 main 容器日志。"""

        pods = [
            _pod("wf-x-aaa", node_name="wf-x[0].s1"),
            _pod("wf-x-bbb", node_name="wf-x[1].s2"),
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/api/v1/namespaces/ns/pods" in url and "/log" not in url:
                return httpx.Response(200, json={"items": pods})
            if url.endswith("/log") or "/log?" in url:
                pod = request.url.path.split("/")[-2]
                return httpx.Response(200, text=f"line from {pod}\nsecond\n")
            return httpx.Response(404)

        c = _make_client(handler)
        try:
            lines = []
            async for line in c.stream_logs(namespace="ns", argo_name="wf-x"):
                lines.append(line)
            # 两个 pod 都拉到，按名排序（aaa 在 bbb 前）
            joined = "".join(lines)
            assert "line from wf-x-aaa" in joined
            assert "line from wf-x-bbb" in joined
            assert "second" in joined
        finally:
            await c.close()

    async def test_step_name_filters_by_node_name_annotation(self):
        """step_name=<node 名> → 仅返回 annotation 匹配那个 pod 的日志。

        这是 Fix 2 的核心：旧代码 step_name in pod_name，而 node 名
        （wf-x[0].s1）不是 pod 名（wf-x-aaa）的子串，恒命中 0 pod → 误导性 502。
        """

        pods = [
            _pod("wf-x-aaa", node_name="wf-x[0].s1"),
            _pod("wf-x-bbb", node_name="wf-x[1].s2"),
        ]
        requested_pod = {}

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/api/v1/namespaces/ns/pods" in url and "/log" not in url:
                return httpx.Response(200, json={"items": pods})
            if "/log" in url:
                pod = request.url.path.split("/")[-2]
                requested_pod[pod] = True
                return httpx.Response(200, text=f"only {pod}\n")
            return httpx.Response(404)

        c = _make_client(handler)
        try:
            lines = []
            async for line in c.stream_logs(
                namespace="ns", argo_name="wf-x", step_name="wf-x[0].s1"
            ):
                lines.append(line)
            joined = "".join(lines)
            assert "wf-x-aaa" in joined
            assert "wf-x-bbb" not in joined
            # 只请求了匹配的 pod
            assert list(requested_pod) == ["wf-x-aaa"]
        finally:
            await c.close()

    async def test_no_pods_for_workflow_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"items": []})

        from workflow_svc.argo_client import ArgoError

        c = _make_client(handler)
        try:
            with pytest.raises(ArgoError, match="no pods found"):
                async for _ in c.stream_logs(namespace="ns", argo_name="wf-x"):
                    pass
        finally:
            await c.close()


# ============ resume（via argo-server） ============


class TestResumeViaArgoServer:
    """resume 经 argo-server REST（D1）+ 始终发 SA token（D2）。"""

    async def test_resume_puts_to_argo_server_with_token(self):
        seen: dict = {}

        def server_handler(request: httpx.Request) -> httpx.Response:
            seen["method"] = request.method
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization")
            seen["body"] = request.content
            return httpx.Response(200, json={"metadata": {"name": "wf-x"}})

        def crd_handler(request: httpx.Request) -> httpx.Response:  # resume 不该触达 CRD
            return httpx.Response(404)

        c = _make_client(crd_handler, server_handler)
        try:
            await c.resume(namespace="apihub-workflow", argo_name="wf-x")
            # method 钉死 PUT：Argo grpc-gateway resume 只注册 PUT，POST→501 UNIMPLEMENTED
            # （Task 2 live smoke 踩过；此断言防 post→put 回归）。
            assert seen["method"] == "PUT"
            assert "/api/v1/workflows/apihub-workflow/wf-x/resume" in seen["url"]
            assert seen["auth"] == "Bearer fake-token"
            assert seen["body"] == b"{}"
        finally:
            await c.close()

    async def test_resume_202_accepted(self):
        def server_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(202, json={})

        c = _make_client(lambda r: httpx.Response(404), server_handler)
        try:
            await c.resume(namespace="ns", argo_name="wf-x")  # 不抛即过
        finally:
            await c.close()

    async def test_resume_500_raises_argo_error(self):
        from workflow_svc.argo_client import ArgoError

        def server_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        c = _make_client(lambda r: httpx.Response(404), server_handler)
        try:
            with pytest.raises(ArgoError, match="argo-server resume returned 500"):
                await c.resume(namespace="ns", argo_name="wf-x")
        finally:
            await c.close()


async def test_init_no_verify_deprecation_warning():
    """verify=<str> 已改 ssl context —— 构造不再触发 httpx DeprecationWarning。"""
    import warnings

    import certifi

    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        c = K8sArgoClient(token="t", ca_cert_path=certifi.where())  # noqa: S106
    await c.close()
