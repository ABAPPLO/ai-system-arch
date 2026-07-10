# deploy/k8s/services —— 各服务 base（⚠️ 禁止直接 kubectl apply）

每个子目录 `<svc>/{configmap,deployment}.yaml` 是该服务的 kustomize **base**（prod 默认形态）。
dev/kind 环境的差异（envFrom 引 apihub-shared-infra、ARGO_MODE=k8s、副本数、host IP）由
`deploy/k8s/overlays/<env>/patches/` 注入。

**禁止** `kubectl apply -f deploy/k8s/services/<svc>/configmap.yaml`：会绕过 kustomize，
revert overlay patch（如把 ARGO_MODE 从 k8s 打回 stub、抹掉 envFrom）→ pod 静默 crash
（`pg_user Field required` / workflow stub 模式）。

**改单个服务后重新 apply 的正确方式**：`make k8s-apply-kind`（或对应 env）→ `make k8s-check-kind`。
若只改了镜像，先 `kind load docker-image` 再 `kubectl rollout restart deploy/<svc>`。
