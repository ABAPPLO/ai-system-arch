# RDS PostgreSQL（主备跨 AZ + SQL 审计）— 详见 docs/04-data-model.md §3 + docs/09-deployment.md §1.3

resource "alicloud_db_instance" "this" {
  engine                   = "PostgreSQL"
  engine_version           = "16.0"
  db_instance_storage_type = "cloud_essd"
  instance_type            = var.instance_type
  instance_storage         = var.storage
  vswitch_id               = var.vswitch_id_a
  # 按量付费（曾写成恒真三元，收敛为单值；若 prod 转包年包月再显式改 Prepaid）
  instance_charge_type = "Postpaid"
  category             = "HighAvailability"
  zone_id_slave_a      = data.alicloud_vswitches.b.vswitches[0].zone_id

  security_group_ids = [var.security_group_id]

  parameters {
    name  = "rds.force_ssl"
    value = "on"
  }

  parameters {
    name  = "log_statement"
    value = "ddl"
  }

  dynamic "parameters" {
    for_each = var.logical_replication ? ["on"] : []
    content {
      name  = "rds.logical_replication"
      value = parameters.value
    }
  }
}

data "alicloud_vswitches" "b" {
  ids = [var.vswitch_id_b]
}

# ⚠️ 这是「迁移/管理」账号（默认名 apihub），只跑 init-db / 迁移，不是业务流量账号：
# 业务账号 apihub_app 由 scripts/init-db/00-roles.sql 创建（NOSUPERUSER NOBYPASSRLS，
# RLS 不变量），K8s 侧 PG_USER=apihub_app。勿把它当 app 账号收紧，也勿给 app 账号 Super。
resource "alicloud_rds_account" "this" {
  db_instance_id   = alicloud_db_instance.this.id
  account_name     = var.username
  account_password = var.password
  account_type     = "Super"
}

resource "alicloud_db_database" "this" {
  instance_id = alicloud_db_instance.this.id
  name        = var.database_name
}

# 备份策略（docs/09-deployment.md §7.1 承诺：每日全备 + 日志备份 ≈ RPO 0）。
# 此前完全缺失 —— 元数据（apis/apps/keys/租户）丢失不可恢复。跨 Region 备份OSS
# 复制（backup_archive / cross_region）阿里云侧需在控制台或 CLI 开启，见 docs/09。
resource "alicloud_db_backup_policy" "this" {
  instance_id = alicloud_db_instance.this.id

  backup_period    = var.backup_period
  backup_time      = var.backup_time
  retention_period = var.backup_retention_days

  # 增量日志备份：全备之间的写入不丢（没有它 RPO = 全备间隔，最长 24h）
  enable_backup_log           = true
  log_backup_retention_period = var.backup_log_retention_days
}

# SQL 审计（SQL 洞察）：alicloud provider 暂无对应 resource，通过 SLS 日志投递在控制台/CLI 侧配置。
