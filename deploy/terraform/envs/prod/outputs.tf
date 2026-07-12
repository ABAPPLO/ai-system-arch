# prod outputs —— 暴露给 Task 2 overlay ConfigMap（PG_HOST / REDIS_HOST / KAFKA_BROKERS / kubeconfig）
# output 名（左侧）按 brief 固定；value 引用的字段名以各 module 实际 outputs.tf 为准（已对齐）

output "kubeconfig" {
  description = "prod ACK kubeconfig"
  value       = module.ack.kubeconfig
  sensitive   = true
}

output "rds_endpoint" {
  description = "RDS 连接地址（喂 overlay PG_HOST）"
  value       = module.rds.connection_string
}

output "redis_host" {
  description = "Redis 连接地址（喂 overlay REDIS_HOST）"
  # redis 模块实际 output 名为 connection_domain（无 host）
  value = module.redis.connection_domain
}

output "kafka_brokers" {
  description = "Kafka 接入域名（喂 overlay KAFKA_BROKERS）"
  # kafka 模块实际 output 名为 domain（无 bootstrap_brokers）
  value = module.kafka.domain
}
