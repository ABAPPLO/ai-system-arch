# staging 环境 — 组合各模块（mirror prod，staging 规格，介于 dev 与 prod 之间）
# 详见 docs/09-deployment.md §1 + §2
# 与 dev 的差异：ACK 节点数 5→6；RDS 存储 100→150GB；Redis master.small→medium；OSS bucket 名带 staging
# 与 prod 的差异：ACK 节点数 6<8、规格 ecs.c7.2xlarge<4xlarge；RDS pg.n2.medium.2c<large.2c、存储 150<200GB；
#                 Redis master.medium<large；backend 独立

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
  # staging：节点数 6（dev 5 < staging 6 < prod 8），规格 ecs.c7.2xlarge（dev 同款，prod 升至 ecs.c7.4xlarge）
  node_count         = 6
  node_instance_type = "ecs.c7.2xlarge"
}

module "rds" {
  source            = "../../modules/rds"
  environment       = var.environment
  vswitch_id_a      = module.vpc.data_vswitch_ids[0]
  vswitch_id_b      = module.vpc.data_vswitch_ids[1]
  security_group_id = module.vpc.security_group_ids.data
  # staging：pg.n2.medium.2c（dev 同款，prod 升至 large.2c）；存储 150GB（dev 100 < staging 150 < prod 200）
  instance_type = "pg.n2.medium.2c"
  storage       = 150
  password      = var.rds_password
}

module "redis" {
  source            = "../../modules/redis"
  environment       = var.environment
  vswitch_id        = module.vpc.data_vswitch_ids[0]
  security_group_id = module.vpc.security_group_ids.data
  # staging：redis.master.medium.default（dev small < staging medium < prod large）
  instance_class = "redis.master.medium.default"
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
