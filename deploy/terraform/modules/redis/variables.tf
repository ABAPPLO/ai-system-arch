variable "environment" {
  type = string
}

variable "vswitch_id" {
  type = string
}

variable "security_group_id" {
  type = string
}

variable "instance_class" {
  description = "Redis 规格，dev 起步 1G master"
  type        = string
  default     = "redis.master.small.default"
}
