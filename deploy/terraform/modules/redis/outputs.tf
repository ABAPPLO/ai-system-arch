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
  value = alicloud_kvstore_instance.this.user_name
}
