# Redis 集群版 — 限流 / 缓存 / 配额（key 前缀 `t:{tenant_id}:`）

resource "alicloud_kvstore_instance" "this" {
  name                = "apihub-${var.environment}-redis"
  instance_class      = var.instance_class
  vswitch_id          = var.vswitch_id
  security_group_id   = var.security_group_id
  instance_type       = "Redis"
  engine_version      = "7.0"
  charge_type         = "PostPaid"
  availability_zone   = data.alicloud_vswitch.this.zone_id
  auto_renew_period   = 0
  instance_release_protection = var.environment == "prod" ? true : false
}

data "alicloud_vswitch" "this" {
  id = var.vswitch_id
}
