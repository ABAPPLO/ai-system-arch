# prod-bj（北京）环境 — 多活第二站，与 prod（上海）通过 VPC Peering 互通
# 详见 docs/09-deployment.md §1 + §2
# 与 prod 的差异：vpc_cidr 改用 10.1.0.0/16（多活 CIDR 规划见 docs/00-decisions.md §多活）；
#                ACK 节点数 15（BJs 接入一半流量）；RDS 升档 pg.x4.large.2c + 500GB
#                + logical_replication 开（DTS 跨 Region 同步）；VPC Peering 连上海

module "vpc" {
  source             = "../../modules/vpc"
  environment        = var.environment
  region             = var.region
  availability_zones = ["cn-beijing-h", "cn-beijing-i", "cn-beijing-j"]
  vpc_cidr           = "10.1.0.0/16"
  dmz_cidr           = "10.1.1.0/24"
  app_cidr           = "10.1.10.0/24"
  data_cidr          = "10.1.20.0/24"
  mgmt_cidr          = "10.1.99.0/24"
  enable_peering     = true
  peer_vpc_id        = var.peer_vpc_id
  peer_region        = "cn-shanghai"
}

module "ack" {
  source            = "../../modules/ack"
  environment       = var.environment
  vpc_id            = module.vpc.vpc_id
  vswitch_ids       = module.vpc.app_vswitch_ids
  security_group_id = module.vpc.security_group_ids.app
  # prod-bj：节点数 15（BJ 承担一半流量），规格 ecs.c7.2xlarge（与 prod 的 4xlarge 不同）
  node_count         = 15
  node_instance_type = "ecs.c7.2xlarge"
}

module "rds" {
  source             = "../../modules/rds"
  environment        = var.environment
  vswitch_id_a       = module.vpc.data_vswitch_ids[0]
  vswitch_id_b       = module.vpc.data_vswitch_ids[1]
  security_group_id  = module.vpc.security_group_ids.data
  # prod-bj：pg.x4.large.2c（计算型升档应对跨 Region 同步压力）；存储 500GB
  # logical_replication 开启以便 DTS 做跨 Region 逻辑复制
  instance_type       = "pg.x4.large.2c"
  storage             = 500
  password            = var.rds_password
  logical_replication = true
}

module "redis" {
  source            = "../../modules/redis"
  environment       = var.environment
  vswitch_id        = module.vpc.data_vswitch_ids[0]
  security_group_id = module.vpc.security_group_ids.data
  # prod-bj：redis.master.large.default（同 prod 规格）
  instance_class = "redis.master.large.default"
}

module "kafka" {
  source            = "../../modules/kafka"
  environment       = var.environment
  vswitch_id        = module.vpc.data_vswitch_ids[0]
  security_group_id = module.vpc.security_group_ids.data
}

module "oss" {
  source      = "../../modules/oss"
  environment = var.environment
  bucket_name = "apihub-prod-bj-objects"
}
