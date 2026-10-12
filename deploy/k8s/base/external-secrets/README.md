# Secret 供给方案：External Secrets Operator + 阿里云 KMS 凭据管家

> 位置：各云 overlay（`deploy/k8s/overlays/{staging,prod,prod-bj}/external-secrets.yaml`）。
> kind/dev 不走此方案（kind 直接 patch 固定 dev 值，见 `overlays/kind/patches/`）。

## 为什么

此前 Secret 以 `REPLACE_ME` 占位提交进 Git，约定「apply 时填真值」——但 ArgoCD 开了
`automated + selfHeal + prune`，手工填的值会被 Git 状态立刻回滚；把真值提交进 Git 则泄漏。
ESO 让 Git 只描述**映射**（KMS 凭据名 → K8s Secret 字段），值始终留在 KMS：

- `apihub-shared-secret-{env}`：`PG_PASSWORD` / `JWT_SECRET` / `REDIS_PASSWORD` / `CH_PASSWORD`
- `apihub-hmac-key`：`hmac_secret_key`（HMAC envelope key，auth/dispatcher/notification 共享；
  **prod 与 prod-bj 必须同值**——跨区 webhook 验签一致——故挂 `apihub/shared/hmac_secret_key`）

## 首次启用步骤（每环境一次）

1. **装 ESO controller**（v0.9.19+，需含 alibaba provider）：

   ```bash
   helm repo add external-secrets https://charts.external-secrets.io
   helm install external-secrets external-secrets/external-secrets \
     -n external-secrets --create-namespace --version 0.9.19
   ```

2. **创建 KMS 凭据**（凭据管家，`env` ∈ staging / prod / prod-bj）：

   ```
   apihub/{env}/pg_password      # RDS apihub_app 账号密码（RDS 侧由 terraform rds_password 同步）
   apihub/{env}/jwt_secret       # JWT 签名密钥（auth/portal/admin）
   apihub/{env}/redis_password
   apihub/{env}/ch_password
   apihub/shared/hmac_secret_key # 64 hex（AES-256 envelope key），两生产区同值
   apihub/shared/peer_ch_password_bj  # prod 访问北京 CH（PEER_REGION_CH_PASSWORD）
   apihub/shared/peer_ch_password_sh  # prod-bj 访问上海 CH
   apihub/shared/smtp_password   # 平台发信 SMTP 密码（钉钉企业邮箱；notification）
   ```

3. **创建 ESO 访问凭据**：RAM 子账号（只授权 KMS 凭据管家读），AK 写入
   `apihub-system/alicloud-eso-credentials`（`access-key-id` / `access-key-secret` 两键）。
   生产建议改 RRSA（ACK 节点角色assume，免静态 AK）——修改
   `external-secrets.yaml` 里 `auth.secretRef` → `auth.rrsa`。

4. `kubectl apply -k deploy/k8s/overlays/<env>`（或推 Git 由 ArgoCD 同步），确认：

   ```bash
   kubectl -n apihub-system get externalsecret
   # STATUS 应为 SecretSynced；Secret apihub-shared-secret-<env> 已生成
   ```

## 校验

`scripts/validate-overlay-coverage.py` 会在渲染产物中断言：不再出现 `REPLACE_ME` 的
Secret 值（防占位手法回潮）。

## 遗留

- `ai-gateway-secret`（AI provider key 加密密钥 `AI_GATEWAY_ENCRYPTION_KEY`）目前仍是
  base 空壳 + kind 显式注入；上生产前同样加一条 ExternalSecret（KMS：
  `apihub/{env}/ai_gateway_key`），注意 base 空壳需移除或改 `creationPolicy: Orphan` 避免双 owner。
- Terraform 侧 `var.rds_password` 与 KMS `apihub/{env}/pg_password` 需保持一致
  （建议 terraform 用 KMS 数据源回读，见 docs/09-deployment.md）。
