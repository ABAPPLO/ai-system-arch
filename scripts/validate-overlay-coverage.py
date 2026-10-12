#!/usr/bin/env python3
"""校验云环境 kustomize overlay 的服务覆盖完整性（渲染时静态检查）。

CI 集成用：
    python scripts/validate-overlay-coverage.py

背景：billing/notification/ai-gateway/portal 四个服务曾只在 kind overlay 补齐
（ENV/envFrom/images），staging/prod/prod-bj 漏配 —— 生产部署时镜像停在
registry.apihub.internal:*-dev（必 ImagePullBackOff）且 configmap 硬编码
ENV=dev（validate_security 的生产密钥校验被绕过）。本脚本把这类「新增服务
只修了 kind」的遗漏变成 CI 红灯。

校验项（staging / prod / prod-bj 三个 overlay）：
  1. deploy/k8s/services/ 下每个有 deployment.yaml 的服务，
     都出现在 overlay 的 resources 且渲染为 Deployment/Rollout
  2. 镜像已改写为 ACR（不含 registry.apihub.internal，不带 -dev tag）
  3. 容器 envFrom 追加了该环境的 apihub-shared-infra-{env} CM
  4. ConfigMap {svc}-config 的 ENV == 环境名
  5. workflow-config 的 ARGO_MODE == k8s（base 是 stub，仅 dev/kind 用）
  6. 渲染产物中不残留 REPLACE_ME（Secret 占位手法已废，见 base/external-secrets/README.md）

kind/dev 不在本脚本范围（kind 镜像由 kind load 注入，dev overlay 是占位）。

失败返回非 0。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("PyYAML not installed: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

REPO = Path(__file__).resolve().parent.parent
OVERLAYS = ("staging", "prod", "prod-bj")
SHARED_CM = {
    "staging": "apihub-shared-infra-staging",
    "prod": "apihub-shared-infra-prod",
    "prod-bj": "apihub-shared-infra-bj",
}


def discover_services() -> list[str]:
    """deploy/k8s/services/ 下所有有 deployment.yaml 的服务名。"""
    root = REPO / "deploy" / "k8s" / "services"
    return sorted(p.parent.name for p in root.glob("*/deployment.yaml"))


def build(env: str) -> list[dict]:
    proc = subprocess.run(
        [
            "kustomize",
            "build",
            str(REPO / "deploy" / "k8s" / "overlays" / env),
            "--load-restrictor",
            "LoadRestrictionsNone",
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        print(f"kustomize build {env} 失败:\n{proc.stderr}", file=sys.stderr)
        sys.exit(1)
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


def main() -> int:
    services = discover_services()
    print(f"发现 {len(services)} 个服务: {', '.join(services)}")
    failures: list[str] = []

    for env in OVERLAYS:
        docs = build(env)
        deployments = {
            d["metadata"]["name"]: d for d in docs if d.get("kind") in ("Deployment", "Rollout")
        }
        configmaps = {d["metadata"]["name"]: d for d in docs if d.get("kind") == "ConfigMap"}

        for svc in services:
            dep = deployments.get(svc)
            if dep is None:
                failures.append(f"[{env}] {svc}: Deployment 未出现在渲染结果（漏 resources?）")
                continue

            spec = dep["spec"]["template"]["spec"]
            image = spec["containers"][0]["image"]
            if "registry.apihub.internal" in image or image.rstrip(":").endswith("-dev"):
                failures.append(f"[{env}] {svc}: 镜像未改写为 ACR（{image}）")

            envfrom = spec["containers"][0].get("envFrom", [])
            cm_names = [e.get("configMapRef", {}).get("name", "") for e in envfrom]
            if SHARED_CM[env] not in cm_names:
                failures.append(f"[{env}] {svc}: envFrom 缺 {SHARED_CM[env]}（现有 {cm_names}）")

            cm = configmaps.get(f"{svc}-config")
            if cm is None:
                failures.append(f"[{env}] {svc}: ConfigMap {svc}-config 未渲染")
            elif cm.get("data", {}).get("ENV") != env:
                failures.append(
                    f"[{env}] {svc}: configmap ENV={cm['data'].get('ENV')!r}"
                    "（应为环境名，dev 值会导致 validate_security 被绕过）"
                )

        for sec in (d for d in docs if d.get("kind") == "Secret"):
            for k, v in (sec.get("stringData") or {}).items():
                if isinstance(v, str) and "REPLACE_ME" in v:
                    failures.append(
                        f"[{env}] Secret {sec['metadata']['name']} 字段 {k} 仍是 REPLACE_ME 占位"
                        "（应走 external-secrets，见 deploy/k8s/base/external-secrets/README.md）"
                    )

        wf = configmaps.get("workflow-config")
        if wf is None:
            failures.append(f"[{env}] workflow-config ConfigMap 未渲染")
        elif wf.get("data", {}).get("ARGO_MODE") != "k8s":
            failures.append(
                f"[{env}] ARGO_MODE={wf['data'].get('ARGO_MODE')!r}（生产必须 k8s，stub=假执行）"
            )

    if failures:
        print(f"\n❌ overlay 覆盖检查失败（{len(failures)} 项）:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("✅ staging/prod/prod-bj 覆盖完整（镜像/envFrom/ENV/ARGO_MODE）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
