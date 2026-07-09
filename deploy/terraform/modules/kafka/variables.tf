variable "environment" {
  type = string
}

variable "vswitch_id" {
  type = string
}

variable "security_group_id" {
  type = string
}

variable "instance_type" {
  description = "Kafka broker 规格"
  type        = string
  default     = "elasticsearch.sn1ne.large"
}

variable "disk_size" {
  type    = number
  default = 500
}

variable "topics" {
  description = "初始化 topic 列表（分区数、副本数、保留时长小时）"
  type = list(object({
    name            = string
    partition_count = number
    replica_count   = number
    retention_hours = number
  }))
  default = [
    { name = "api-call-events", partition_count = 12, replica_count = 3, retention_hours = 168 },
    { name = "task-requests", partition_count = 6, replica_count = 3, retention_hours = 72 },
    { name = "task-status", partition_count = 6, replica_count = 3, retention_hours = 72 },
    { name = "retry-requests", partition_count = 6, replica_count = 3, retention_hours = 72 },
    { name = "audit-events", partition_count = 3, replica_count = 3, retention_hours = 720 },
    { name = "notification", partition_count = 3, replica_count = 3, retention_hours = 168 },
  ]
}
