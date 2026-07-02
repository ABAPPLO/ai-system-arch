# dev 环境 — 组合各模块
# 详见 docs/09-deployment.md §1 + §2

module "vpc" {
  source           = "../../modules/vpc"
  environment      = var.environment
  region           = var.region
  availability_zones = ["cn-shanghai-e", "cn-shanghai-f", "cn-shanghai-g"]
}

module "ack" {
  source            = "../../modules/ack"
  environment       = var.environment
  vpc_id            = module.vpc.vpc_id
  vswitch_ids       = module.vpc.app_vswitch_ids
  security_group_id = module.vpc.security_group_ids.app
  node_count        = 5
}

module "rds" {
  source            = "../../modules/rds"
  environment       = var.environment
  vswitch_id_a      = module.vpc.data_vswitch_ids[0]
  vswitch_id_b      = module.vpc.data_vswitch_ids[1]
  security_group_id = module.vpc.security_group_ids.data
  instance_type     = "pg.n2.medium.2c"
  storage           = 100
  password          = var.rds_password
}

module "redis" {
  source            = "../../modules/redis"
  environment       = var.environment
  vswitch_id        = module.vpc.data_vswitch_ids[0]
  security_group_id = module.vpc.security_group_ids.data
  instance_class    = "redis.master.small.default"
}

module "kafka" {
  source            = "../../modules/kafka"
  environment       = var.environment
  vswitch_id        = module.vpc.data_vswitch_ids[0]
  security_group_id = module.vpc.security_group_ids.data
}

module "oss" {
  source       = "../../modules/oss"
  environment  = var.environment
  bucket_name  = "apihub-${var.environment}-objects"
}
