variable "region" {
  description = "阿里云 Region"
  type        = string
  default     = "cn-shanghai"
}

variable "environment" {
  description = "环境标识（dev/staging/prod）"
  type        = string
}

variable "vpc_cidr" {
  description = "VPC CIDR"
  type        = string
  default     = "10.0.0.0/16"
}

variable "availability_zones" {
  description = "使用的可用区列表（3 个跨 AZ 高可用）"
  type        = list(string)
  default     = ["cn-shanghai-e", "cn-shanghai-f", "cn-shanghai-g"]
}

variable "dmz_cidr" {
  description = "DMZ 子网（公网入口：SLB / WAF / EIP）"
  type        = string
  default     = "10.0.1.0/24"
}

variable "app_cidr" {
  description = "App 子网（ACK 节点）"
  type        = string
  default     = "10.0.10.0/24"
}

variable "data_cidr" {
  description = "Data 子网（RDS / Redis / Kafka / ClickHouse，无公网）"
  type        = string
  default     = "10.0.20.0/24"
}

variable "mgmt_cidr" {
  description = "Mgmt 子网（堡垒机 / VPN / KMS）"
  type        = string
  default     = "10.0.99.0/24"
}
