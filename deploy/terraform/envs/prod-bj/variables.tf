variable "region" {
  type    = string
  default = "cn-beijing"
}

variable "environment" {
  type    = string
  default = "prod-bj"
}

variable "rds_password" {
  description = "RDS 主账号密码，从环境变量 TF_VAR_rds_password 注入"
  type        = string
  sensitive   = true
}

variable "peer_vpc_id" {
  description = "对端 VPC ID（上海 prod 环境的 VPC ID），用于跨 Region VPC Peering"
  type        = string
}
