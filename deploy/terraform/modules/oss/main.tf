# OSS — 调用 body / 错误堆栈 / SDK 包 / 备份归档
# 强制 SSE-KMS 加密（等保 2.0 三级）

resource "alicloud_oss_bucket" "this" {
  bucket = var.bucket_name
  acl    = "private"

  server_side_encryption_rule {
    sse_algorithm = "KMS"
  }

  versioning {
    status = "Enabled"
  }

  lifecycle_rule {
    id      = "archive-old-versions"
    enabled = true

    noncurrent_version_transition {
      days          = 30
      storage_class = "IA"
    }

    noncurrent_version_transition {
      days          = 90
      storage_class = "Archive"
    }

    noncurrent_version_expiration {
      days = 365
    }
  }
}
