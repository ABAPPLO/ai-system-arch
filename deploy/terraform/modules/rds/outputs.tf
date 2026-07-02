output "db_instance_id" {
  value = alicloud_db_instance.this.id
}

output "connection_string" {
  value = alicloud_db_instance.this.connection_string
}

output "port" {
  value = alicloud_db_instance.this.port
}

output "database_name" {
  value = var.database_name
}

output "username" {
  value = var.username
}
