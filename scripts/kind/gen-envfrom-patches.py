#!/usr/bin/env python3
"""生成 overlays/kind/patches/<svc>-envfrom.yaml（11 个），向 envFrom 追加共享 infra/secret。

用 JSON6902 的 add 到 list 末尾（path 以 /- 结尾），kustomize strategic-merge 会整体替换
list，故这里不用 strategic-merge，而用 patches + JSON patch 确保是 append。

base deployment 已有 envFrom（见 services/<svc>/deployment.yaml），故 add 到 /envFrom/-
成立。若某服务 base 无 envFrom，需先 add /envFrom（空 list）再 append。
"""

import pathlib

SERVICES = [
    "api-registry",
    "dispatcher",
    "auth",
    "executor",
    "quota",
    "tenant",
    "admin",
    "docs",
    "trace",
    "retry",
    "workflow",
]
OUT = pathlib.Path("deploy/k8s/overlays/kind/patches")
OUT.mkdir(parents=True, exist_ok=True)

# JSON6902 patch：把 apihub-shared-infra / apihub-shared-secret 追加到 envFrom 末尾。
# 后者（secret）覆盖前者（configmap）覆盖 base configmap —— 末尾优先级最高。
#
# 文件只含 JSON6902 ops，不含 apiVersion/kind/metadata 头 —— 资源选择由
# kustomization.yaml 里 patches[].target 负责。混用 strategic-merge 头 + JSON6902 ops
# 会让 kustomize 报 "unable to parse SM or JSON patch"。
PATCH_OPS = """- op: add
  path: /spec/template/spec/containers/0/envFrom/-
  value:
    configMapRef:
      name: apihub-shared-infra
- op: add
  path: /spec/template/spec/containers/0/envFrom/-
  value:
    secretRef:
      name: apihub-shared-secret
"""

for svc in SERVICES:
    (OUT / f"{svc}-envfrom.yaml").write_text(PATCH_OPS)

print(f"wrote {len(SERVICES)} patches to {OUT}")
