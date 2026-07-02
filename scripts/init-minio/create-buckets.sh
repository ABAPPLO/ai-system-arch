#!/usr/bin/env bash
# 初始化 MinIO：创建 4 个 bucket（与 docs/04-data-model.md §8 对齐）
#   call-bodies       —— 调用 body / 错误堆栈（按 trace_id 索引）
#   sdk-packages      —— 自动生成的 SDK 包
#   audit-archive     —— 审计归档（>6 月）
#   tfstate           —— Terraform state（dev 用，prod 走 OSS）

set -euo pipefail

ENDPOINT="${MINIO_ENDPOINT:-http://minio:9000}"
USER="${MINIO_USER:-apihub}"
PASSWORD="${MINIO_PASSWORD:-apihub_dev_pwd}"

mc alias set local "$ENDPOINT" "$USER" "$PASSWORD" --api S3v4

for bucket in call-bodies sdk-packages audit-archive tfstate; do
    echo "==> Creating bucket: $bucket"
    mc mb "local/$bucket" --ignore-existing
    # 私有 ACL（不公开）
    mc anonymous set none "local/$bucket" >/dev/null
    # 启用版本管理（误删恢复）
    mc version enable "local/$bucket" >/dev/null
done

echo "==> Buckets ready:"
mc ls local

# 写入一个示例对象验证
echo "hello from apihub-dev" | mc pipe local/call-bodies/_smoketest.txt
mc cat local/call-bodies/_smoketest.txt
mc rm local/call-bodies/_smoketest.txt
