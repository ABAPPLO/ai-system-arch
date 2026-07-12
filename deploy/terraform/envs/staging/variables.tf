variable "region" {
  type    = string
  default = "cn-shanghai"
}

variable "environment" {
  type    = string
  default = "staging"
}

variable "rds_password" {
  description = "RDS 主账号密码，从环境变量 TF_VAR_rds_password 注入"
  type        = string
  sensitive   = true
}
