"""Argo Workflow K8s 客户端 —— 提交 / 查询 / 取消 / 恢复 / 步骤 / 日志。

两种模式：
  - mode="stub"：内存模拟，给 dev / test 用，不依赖真 K8s
  - mode="k8s"：走 K8s API server，调 argoproj.io/v1alpha1 Workflow CRD

模式由 settings.argo_mode 决定（默认 stub，prod 才开 k8s）。

K8s 模式下用 httpx + Bearer token（pod serviceaccount 自动挂载），
不依赖 kubernetes python client —— 一个轻量 httpx 调用就够。
"""

import random
import string
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
from apihub_core.logging import get_logger

from workflow_svc.models import (
    StepStatus,
    WorkflowStatus,
    WorkflowStep,
)

log = get_logger(__name__)


# ============ 异常 ============

class ArgoError(Exception):
    """Argo / K8s API 调用失败。"""


# ============ 抽象接口 ============

class ArgoClient:
    """Argo 客户端抽象。子类：StubArgoClient / K8sArgoClient。"""

    async def submit(
        self,
        *,
        namespace: str,
        workflow_uuid: str,
        spec: dict,
        labels: dict[str, str],
    ) -> str:
        """提交 workflow，返回 Argo workflow name。"""
        raise NotImplementedError

    async def get_status(
        self, *, namespace: str, argo_name: str
    ) -> tuple[WorkflowStatus, str | None]:
        """查询 workflow 状态。返回 (status, message)。"""
        raise NotImplementedError

    async def cancel(
        self, *, namespace: str, argo_name: str
    ) -> None:
        raise NotImplementedError

    async def resume(
        self, *, namespace: str, argo_name: str
    ) -> None:
        raise NotImplementedError

    async def get_steps(
        self, *, namespace: str, argo_name: str
    ) -> list[WorkflowStep]:
        raise NotImplementedError

    async def stream_logs(
        self, *, namespace: str, argo_name: str, step_name: str | None = None
    ) -> AsyncIterator[str]:
        """异步迭代日志行。子类用 yield。"""
        raise NotImplementedError

    async def close(self) -> None:
        pass


# ============ Stub 模式（dev / test） ============

class StubArgoClient(ArgoClient):
    """内存模拟 Argo。每次 submit 返回随机 name，状态机简单走过。"""

    def __init__(self):
        self._workflows: dict[str, dict] = {}
        self._counter = 0

    async def submit(
        self,
        *,
        namespace: str,
        workflow_uuid: str,
        spec: dict,
        labels: dict[str, str],
    ) -> str:
        self._counter += 1
        argo_name = f"wf-{workflow_uuid[:8]}-{self._counter}"
        # spec 里找 templates / entrypoint 模拟 step
        steps = self._derive_steps(spec)
        self._workflows[f"{namespace}/{argo_name}"] = {
            "uuid": workflow_uuid,
            "status": WorkflowStatus.RUNNING,
            "message": None,
            "spec": spec,
            "steps": steps,
            "started_at": datetime.now(UTC),
            "finished_at": None,
            "logs": {s.name: [f"[{s.name}] step started\n"] for s in steps},
        }
        log.info("stub_argo_submit", argo_name=argo_name, namespace=namespace)
        return argo_name

    def _derive_steps(self, spec: dict) -> list[WorkflowStep]:
        """从 Argo spec 提取 step 名（简单实现，不解析 DAG）。"""
        steps: list[WorkflowStep] = []
        for tmpl in spec.get("templates", []):
            name = tmpl.get("name")
            if name:
                steps.append(WorkflowStep(
                    name=name,
                    template=name,
                    status=StepStatus.RUNNING,
                    started_at=datetime.now(UTC),
                ))
        return steps

    async def get_status(
        self, *, namespace: str, argo_name: str
    ) -> tuple[WorkflowStatus, str | None]:
        key = f"{namespace}/{argo_name}"
        wf = self._workflows.get(key)
        if wf is None:
            raise ArgoError(f"workflow {argo_name} not found", )
        return wf["status"], wf["message"]

    async def cancel(self, *, namespace: str, argo_name: str) -> None:
        key = f"{namespace}/{argo_name}"
        wf = self._workflows.get(key)
        if wf is None:
            raise ArgoError(f"workflow {argo_name} not found")
        wf["status"] = WorkflowStatus.CANCELLED
        wf["finished_at"] = datetime.now(UTC)
        for s in wf["steps"]:
            if s.status == StepStatus.RUNNING:
                s.status = StepStatus.SKIPPED
                s.finished_at = datetime.now(UTC)

    async def resume(self, *, namespace: str, argo_name: str) -> None:
        key = f"{namespace}/{argo_name}"
        wf = self._workflows.get(key)
        if wf is None:
            raise ArgoError(f"workflow {argo_name} not found")
        wf["status"] = WorkflowStatus.RUNNING
        wf["finished_at"] = None

    async def get_steps(
        self, *, namespace: str, argo_name: str
    ) -> list[WorkflowStep]:
        key = f"{namespace}/{argo_name}"
        wf = self._workflows.get(key)
        if wf is None:
            raise ArgoError(f"workflow {argo_name} not found")
        return list(wf["steps"])

    async def stream_logs(
        self, *, namespace: str, argo_name: str, step_name: str | None = None
    ) -> AsyncIterator[str]:
        key = f"{namespace}/{argo_name}"
        wf = self._workflows.get(key)
        if wf is None:
            raise ArgoError(f"workflow {argo_name} not found")
        targets = [step_name] if step_name else [s.name for s in wf["steps"]]
        for sn in targets:
            for line in wf["logs"].get(sn, []):
                yield line


# ============ K8s 模式（prod） ============

# Argo Workflow CRD endpoint:
#   /apis/argoproj.io/v1alpha1/namespaces/{ns}/workflows
class K8sArgoClient(ArgoClient):
    """通过 K8s API server 操作 Argo Workflow CRD。

    认证：pod 内自动挂载 serviceaccount token。
    地址：kubernetes.default.svc + 443 (in-cluster)。
    """

    def __init__(
        self,
        *,
        api_server: str = "https://kubernetes.default.svc",
        token: str | None = None,
        ca_cert_path: str = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
        timeout: float = 30.0,
    ):
        self._api_server = api_server
        self._token = token or self._read_sa_token()
        self._timeout = timeout
        self._client = httpx.AsyncClient(
            base_url=api_server,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
            },
            verify=ca_cert_path,
            timeout=httpx.Timeout(connect=5.0, read=timeout, write=5.0, pool=5.0),
        )

    @staticmethod
    def _read_sa_token() -> str:
        try:
            with open("/var/run/secrets/kubernetes.io/serviceaccount/token") as f:
                return f.read().strip()
        except OSError as e:
            raise ArgoError("serviceaccount token not found (not in cluster?)") from e

    async def submit(
        self,
        *,
        namespace: str,
        workflow_uuid: str,
        spec: dict,
        labels: dict[str, str],
    ) -> str:
        # 包成 Argo Workflow CRD
        crd = {
            "apiVersion": "argoproj.io/v1alpha1",
            "kind": "Workflow",
            "metadata": {
                "generateName": f"wf-{workflow_uuid[:8]}-",
                "labels": {
                    "tenant-id": labels.get("tenant_id", ""),
                    "api-id": str(labels.get("api_id", "")),
                    "app-id": str(labels.get("app_id", "")),
                    "trace-id": labels.get("trace_id", ""),
                    "managed-by": "apihub-workflow-svc",
                },
            },
            "spec": spec,
        }
        try:
            resp = await self._client.post(
                f"/apis/argoproj.io/v1alpha1/namespaces/{namespace}/workflows",
                json=crd,
            )
        except httpx.RequestError as e:
            raise ArgoError(f"k8s submit request failed: {e}") from e
        if resp.status_code not in (200, 201):
            raise ArgoError(
                f"k8s submit returned {resp.status_code}: {resp.text[:500]}"
            )
        data = resp.json()
        return data["metadata"]["name"]

    async def get_status(
        self, *, namespace: str, argo_name: str
    ) -> tuple[WorkflowStatus, str | None]:
        try:
            resp = await self._client.get(
                f"/apis/argoproj.io/v1alpha1/namespaces/{namespace}/workflows/{argo_name}"
            )
        except httpx.RequestError as e:
            raise ArgoError(f"k8s get failed: {e}") from e
        if resp.status_code == 404:
            raise ArgoError(f"workflow {argo_name} not found")
        if resp.status_code != 200:
            raise ArgoError(
                f"k8s get returned {resp.status_code}: {resp.text[:500]}"
            )
        data = resp.json()
        phase = data.get("status", {}).get("phase", "")
        msg = data.get("status", {}).get("message")
        return _phase_to_status(phase), msg

    async def cancel(self, *, namespace: str, argo_name: str) -> None:
        # PATCH spec.shutdown
        try:
            resp = await self._client.patch(
                f"/apis/argoproj.io/v1alpha1/namespaces/{namespace}/workflows/{argo_name}",
                headers={"Content-Type": "application/merge-patch+json"},
                json={"spec": {"shutdown": "Stop"}},
            )
        except httpx.RequestError as e:
            raise ArgoError(f"k8s cancel failed: {e}") from e
        if resp.status_code not in (200, 202):
            raise ArgoError(
                f"k8s cancel returned {resp.status_code}: {resp.text[:500]}"
            )

    async def resume(self, *, namespace: str, argo_name: str) -> None:
        # Argo resume 走子资源：POST /workflows/{name}/resume
        try:
            resp = await self._client.post(
                f"/apis/argoproj.io/v1alpha1/namespaces/{namespace}/workflows/{argo_name}/resume",
            )
        except httpx.RequestError as e:
            raise ArgoError(f"k8s resume failed: {e}") from e
        if resp.status_code not in (200, 202):
            raise ArgoError(
                f"k8s resume returned {resp.status_code}: {resp.text[:500]}"
            )

    async def get_steps(
        self, *, namespace: str, argo_name: str
    ) -> list[WorkflowStep]:
        try:
            resp = await self._client.get(
                f"/apis/argoproj.io/v1alpha1/namespaces/{namespace}/workflows/{argo_name}"
            )
        except httpx.RequestError as e:
            raise ArgoError(f"k8s get steps failed: {e}") from e
        if resp.status_code != 200:
            raise ArgoError(
                f"k8s get steps returned {resp.status_code}: {resp.text[:500]}"
            )
        data = resp.json()
        nodes = data.get("status", {}).get("nodes", {})
        return [_node_to_step(n) for n in nodes.values()]

    async def stream_logs(
        self, *, namespace: str, argo_name: str, step_name: str | None = None
    ) -> AsyncIterator[str]:
        # Argo logs endpoint: GET /workflows/{name}/log?podName=...
        # 真实环境用 SSE / chunked；这里简化为一次性拉取后逐行 yield。
        # 后续可以接 httpx.stream 持续读。
        url = (
            f"/apis/argoproj.io/v1alpha1/namespaces/{namespace}"
            f"/workflows/{argo_name}/log"
        )
        params = {}
        if step_name:
            params["podName"] = step_name
        try:
            async with self._client.stream("GET", url, params=params) as resp:
                if resp.status_code != 200:
                    text = await resp.aread()
                    raise ArgoError(
                        f"k8s logs returned {resp.status_code}: {text[:500]!r}"
                    )
                async for line in resp.aiter_lines():
                    yield line + "\n"
        except httpx.RequestError as e:
            raise ArgoError(f"k8s logs failed: {e}") from e

    async def close(self) -> None:
        await self._client.aclose()


def _phase_to_status(phase: str) -> WorkflowStatus:
    """Argo phase → workflow-svc status。"""
    mapping = {
        "Pending": WorkflowStatus.SUBMITTED,
        "Running": WorkflowStatus.RUNNING,
        "Succeeded": WorkflowStatus.SUCCEEDED,
        "Failed": WorkflowStatus.FAILED,
        "Error": WorkflowStatus.FAILED,
        "Skipped": WorkflowStatus.CANCELLED,
        "Stopped": WorkflowStatus.CANCELLED,
    }
    return mapping.get(phase, WorkflowStatus.UNKNOWN)


def _node_to_step(node: dict) -> WorkflowStep:
    """Argo node → WorkflowStep。"""
    name = node.get("name", "unknown")
    phase = node.get("phase", "")
    started = node.get("startedAt")
    finished = node.get("finishedAt")
    msg = node.get("message")

    step_status = {
        "Pending": StepStatus.PENDING,
        "Running": StepStatus.RUNNING,
        "Succeeded": StepStatus.SUCCEEDED,
        "Failed": StepStatus.FAILED,
        "Skipped": StepStatus.SKIPPED,
        "Error": StepStatus.FAILED,
    }.get(phase, StepStatus.UNKNOWN)

    return WorkflowStep(
        name=name,
        template=node.get("templateName", ""),
        status=step_status,
        started_at=_parse_argo_time(started),
        finished_at=_parse_argo_time(finished),
        message=msg,
        inputs=node.get("inputs", {}).get("parameters", []),
        outputs=node.get("outputs", {}).get("parameters", []),
    )


def _parse_argo_time(s: str | None) -> datetime | None:
    """Argo 用 RFC3339，Python datetime.fromisoformat 能直接吃。"""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


# ============ 工厂 ============

_client: ArgoClient | None = None


def get_argo_client() -> ArgoClient:
    """拿当前 client（startup 时 init）。"""
    if _client is None:
        raise RuntimeError("Argo client not initialized. Call init_argo_client first.")
    return _client


def init_argo_client(*, mode: str = "stub", **kwargs) -> ArgoClient:
    """startup 调用。

    mode="stub" → StubArgoClient
    mode="k8s"  → K8sArgoClient（in-cluster serviceaccount）
    """
    global _client
    if mode == "stub":
        _client = StubArgoClient()
    elif mode == "k8s":
        _client = K8sArgoClient(**kwargs)
    else:
        raise ValueError(f"unknown argo_mode: {mode}")
    log.info("argo_client_initialized", mode=mode)
    return _client


async def close_argo_client() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None


# 用于生成 workflow_uuid（公开，方便测试用）
def gen_workflow_uuid() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=16))  # noqa: S311
