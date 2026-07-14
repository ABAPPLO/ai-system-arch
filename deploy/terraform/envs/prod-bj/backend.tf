# 远程 backend：OSS + TableStore（锁）—— prod-bj 独立 state，与 prod 隔离
# 首次需先手动创建 OSS bucket（apihub-tfstate-bj）+ TableStore 表（tflock_prod_bj）
# 详见 docs/09-deployment.md §5.2

terraform {
  backend "oss" {
    bucket  = "apihub-tfstate-bj"
    prefix  = "prod-bj"
    key     = "terraform.tfstate"
    region  = "cn-beijing"
    encrypt = true
    acl     = "private"
  }
}
