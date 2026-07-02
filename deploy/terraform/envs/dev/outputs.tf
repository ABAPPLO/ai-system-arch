output "ack_cluster_id" {
  value = module.ack.cluster_id
}

output "ack_api_server" {
  value = module.ack.api_server_endpoint
}

output "rds_connection" {
  value     = "${module.rds.connection_string}:${module.rds.port}"
  sensitive = false
}

output "rds_database" {
  value = module.rds.database_name
}

output "rds_username" {
  value = module.rds.username
}

output "redis_domain" {
  value = module.redis.connection_domain
}

output "kafka_domain" {
  value = module.kafka.domain
}

output "oss_bucket" {
  value = module.oss.bucket_name
}
