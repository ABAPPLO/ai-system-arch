# deploy/k8s/base —— kustomize base（⚠️ 禁止直接 kubectl apply）

本目录是 kustomize **base** 资源（namespaces / shared / apigw / argo / db-init）。
各环境差异（host IP、ARGO_MODE、envFrom、端口）由 `deploy/k8s/overlays/<env>/` 的 patch 注入。

**禁止** `kubectl apply -f deploy/k8s/base/...`：会绕过 kustomize，用 base 默认值 revert
live 资源已 patch 的字段 → pod 静默 crash（见 `docs/phase2-integration-findings.md`）。

**唯一 apply 入口**：
- 本地 kind：`make k8s-apply-kind`（或 `scripts/kind/bootstrap.sh` 重建整个集群）
- 云环境：`make k8s-apply-{dev,staging,prod}`
- 底层都走 `scripts/k8s/apply.sh <env>`。

apply 后建议自检：`make k8s-check-kind`（kind）。
