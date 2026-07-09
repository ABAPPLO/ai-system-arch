# RDS PostgreSQL（主备跨 AZ + SQL 审计）— 详见 docs/04-data-model.md §3 + docs/09-deployment.md §1.3

resource "alicloud_db_instance" "this" {
  engine                   = "PostgreSQL"
  engine_version           = "16.0"
  db_instance_storage_type = "cloud_essd"
  instance_type            = var.instance_type
  instance_storage         = var.storage
  vswitch_id               = var.vswitch_id_a
  instance_charge_type     = var.environment == "prod" ? "Postpaid" : "Postpaid"
  category                 = "HighAvailability"
  zone_id_slave_a          = data.alicloud_vswitches.b.vswitches[0].zone_id

  security_group_ids = [var.security_group_id]

  parameters {
    name  = "rds.force_ssl"
    value = "on"
  }

  parameters {
    name  = "log_statement"
    value = "ddl"
  }
}

data "alicloud_vswitches" "b" {
  ids = [var.vswitch_id_b]
}

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

# SQL 审计（SQL 洞察）：alicloud provider 暂无对应 resource，通过 SLS 日志投递在控制台/CLI 侧配置。
