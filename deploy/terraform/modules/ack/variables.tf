variable "environment" {
  description = "环境标识"
  type        = string
}

variable "vpc_id" {
  type = string
}

variable "vswitch_ids" {
  description = "ACK 节点使用的 vSwitch（app 子网）"
  type        = list(string)
}

variable "security_group_id" {
  type = string
}

variable "node_instance_type" {
  description = "默认节点规格"
  type        = string
  default     = "ecs.c7.2xlarge"
}

variable "node_count" {
  description = "节点数量（dev 5 / staging 10 / prod 30）"
  type        = number
  default     = 5
}

variable "node_disk_size" {
  type    = number
  default = 120
}

variable "node_disk_category" {
  type    = string
  default = "cloud_essd"
}
