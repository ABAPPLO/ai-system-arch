variable "environment" {
  type = string
}

variable "vswitch_id_a" {
  description = "主可用区 vSwitch"
  type        = string
}

variable "vswitch_id_b" {
  description = "备可用区 vSwitch"
  type        = string
}

variable "security_group_id" {
  type = string
}

variable "instance_type" {
  description = "RDS 规格代号，dev 用 pg.x4.large.2c 起步"
  type        = string
  default     = "pg.n2.medium.2c"
}

variable "storage" {
  description = "存储 GB"
  type        = number
  default     = 200
}

variable "database_name" {
  type    = string
  default = "apihub"
}

variable "username" {
  type    = string
  default = "apihub"
}

variable "password" {
  description = "主账号密码（建议从 Sealed Secrets / KMS 注入，不要硬编码）"
  type        = string
  sensitive   = true
}
