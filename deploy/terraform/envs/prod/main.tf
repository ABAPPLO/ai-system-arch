# prod 环境 — 组合各模块（mirror dev，prod 规格）
# 详见 docs/09-deployment.md §1 + §2
# 与 dev 的差异：ACK 节点数 5→8 + 规格升一档；RDS pg.n2.medium.2c→large.2c、存储 100→200；
#                Redis master.small→large；OSS bucket 名带 prod；backend 独立

module "vpc" {
  source             = "../../modules/vpc"
  environment        = var.environment
  region             = var.region
  availability_zones = ["cn-shanghai-e", "cn-shanghai-f", "cn-shanghai-g"]
}

module "ack" {
  source            = "../../modules/ack"
  environment       = var.environment
  vpc_id            = module.vpc.vpc_id
  vswitch_ids       = module.vpc.app_vswitch_ids
  security_group_id = module.vpc.security_group_ids.app
  # prod：节点数 8（dev 5），规格较 dev 默认 ecs.c7.2xlarge 升一档到 ecs.c7.4xlarge（16c32g）
  node_count         = 8
  node_instance_type = "ecs.c7.4xlarge"
}

module "rds" {
  source            = "../../modules/rds"
  environment       = var.environment
  vswitch_id_a      = module.vpc.data_vswitch_ids[0]
  vswitch_id_b      = module.vpc.data_vswitch_ids[1]
  security_group_id = module.vpc.security_group_ids.data
  # prod：pg.n2.medium.2c → pg.n2.large.2c；存储 100 → 200GB
  instance_type = "pg.n2.large.2c"
  storage       = 200
  password      = var.rds_password
}

module "redis" {
  source            = "../../modules/redis"
  environment       = var.environment
  vswitch_id        = module.vpc.data_vswitch_ids[0]
  security_group_id = module.vpc.security_group_ids.data
  # prod：redis.master.small.default → redis.master.large.default
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
  bucket_name = "apihub-${var.environment}-objects"
}
