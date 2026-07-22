# VPC + 4 子网（DMZ/App/Data/Mgmt）— 等保 2.0 三级网络隔离
# 详见 docs/09-deployment.md §1.2

resource "alicloud_vpc" "this" {
  vpc_name   = "apihub-${var.environment}-vpc"
  cidr_block = var.vpc_cidr
}

resource "alicloud_vswitch" "dmz" {
  count        = length(var.availability_zones)
  vpc_id       = alicloud_vpc.this.id
  cidr_block   = cidrsubnet(var.dmz_cidr, 4, count.index)
  zone_id      = var.availability_zones[count.index]
  vswitch_name = "apihub-${var.environment}-dmz-${count.index + 1}"
}

resource "alicloud_vswitch" "app" {
  count        = length(var.availability_zones)
  vpc_id       = alicloud_vpc.this.id
  cidr_block   = cidrsubnet(var.app_cidr, 4, count.index)
  zone_id      = var.availability_zones[count.index]
  vswitch_name = "apihub-${var.environment}-app-${count.index + 1}"
}

resource "alicloud_vswitch" "data" {
  count        = length(var.availability_zones)
  vpc_id       = alicloud_vpc.this.id
  cidr_block   = cidrsubnet(var.data_cidr, 4, count.index)
  zone_id      = var.availability_zones[count.index]
  vswitch_name = "apihub-${var.environment}-data-${count.index + 1}"
}

resource "alicloud_vswitch" "mgmt" {
  count        = length(var.availability_zones)
  vpc_id       = alicloud_vpc.this.id
  cidr_block   = cidrsubnet(var.mgmt_cidr, 4, count.index)
  zone_id      = var.availability_zones[count.index]
  vswitch_name = "apihub-${var.environment}-mgmt-${count.index + 1}"
}

# 安全组：DMZ（公网入口）
resource "alicloud_security_group" "dmz" {
  name        = "apihub-${var.environment}-sg-dmz"
  vpc_id      = alicloud_vpc.this.id
  description = "DMZ 公网入口（SLB/WAF/EIP）"
}

# 安全组：App（ACK 节点）
resource "alicloud_security_group" "app" {
  name        = "apihub-${var.environment}-sg-app"
  vpc_id      = alicloud_vpc.this.id
  description = "App ACK 节点"
}

resource "alicloud_security_group_rule" "app_ingress_from_dmz" {
  type                     = "ingress"
  ip_protocol              = "all"
  port_range               = "-1/-1"
  source_security_group_id = alicloud_security_group.dmz.id
  security_group_id        = alicloud_security_group.app.id
}

# 安全组：Data（数据库，仅允许 App 访问）
resource "alicloud_security_group" "data" {
  name        = "apihub-${var.environment}-sg-data"
  vpc_id      = alicloud_vpc.this.id
  description = "Data 子网无公网（RDS/Redis/Kafka/ClickHouse）"
}

resource "alicloud_security_group_rule" "data_ingress_from_app" {
  type                     = "ingress"
  ip_protocol              = "all"
  port_range               = "-1/-1"
  source_security_group_id = alicloud_security_group.app.id
  security_group_id        = alicloud_security_group.data.id
}

# 安全组：Mgmt（堡垒机）
resource "alicloud_security_group" "mgmt" {
  name        = "apihub-${var.environment}-sg-mgmt"
  vpc_id      = alicloud_vpc.this.id
  description = "Mgmt（堡垒机/VPN/KMS）"
}

# 跨 Region VPC Peering（多活架构：北京 ↔ 上海）
resource "alicloud_vpc_peer_connection" "this" {
  count               = var.enable_peering ? 1 : 0
  vpc_id              = alicloud_vpc.this.id
  accepting_vpc_id    = var.peer_vpc_id
  accepting_region_id = var.peer_region
  bandwidth           = 1000
}
