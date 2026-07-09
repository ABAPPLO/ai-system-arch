output "instance_id" {
  value = alicloud_kvstore_instance.this.id
}

output "connection_domain" {
  value = alicloud_kvstore_instance.this.connection_domain
}

output "port" {
  value = alicloud_kvstore_instance.this.port
}

output "user_name" {
  # alicloud Redis 默认账号名即实例 ID（无独立 user_name 属性）
  value = alicloud_kvstore_instance.this.id
}
