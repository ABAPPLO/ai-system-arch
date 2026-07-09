output "instance_id" {
  value = alicloud_alikafka_instance.this.id
}

output "domain" {
  value = alicloud_alikafka_instance.this.domain_endpoint
}

output "topics" {
  value = [for t in var.topics : t.name]
}
