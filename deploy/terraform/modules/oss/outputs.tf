output "bucket_name" {
  value = alicloud_oss_bucket.this.id
}

output "bucket_domain" {
  value = alicloud_oss_bucket.this.extranet_endpoint
}
