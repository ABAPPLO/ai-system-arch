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

variable "logical_replication" {
  description = "启用 PostgreSQL 逻辑复制（DTS 跨 Region 同步需要）"
  type        = bool
  default     = false
}

variable "backup_period" {
  description = "自动全备的星期（阿里云要求至少两天）"
  type        = list(string)
  default = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
  ]
}

variable "backup_time" {
  description = "全备窗口（UTC）。默认 18:00Z-19:00Z = 北京时间凌晨 02:00-03:00 低谷"
  type        = string
  default     = "18:00Z-19:00Z"
}

variable "backup_retention_days" {
  description = "全备保留天数（7-730）。dev 默认 7；prod 建议在 envs 里覆写 30+"
  type        = number
  default     = 7
}

variable "backup_log_retention_days" {
  description = "增量日志备份保留天数（7-730），日志备份开启才近似 RPO=0"
  type        = number
  default     = 7
}
