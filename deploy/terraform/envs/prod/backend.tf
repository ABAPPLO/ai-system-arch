# 远程 backend：OSS + TableStore（锁）—— prod 独立 state，与 dev 隔离
# 首次需先手动创建 OSS bucket（apihub-tfstate-prod）+ TableStore 表（tflock_prod）
# 详见 docs/09-deployment.md §5.2

terraform {
  backend "oss" {
    bucket              = "apihub-tfstate-prod"
    prefix              = "terraform/prod"
    region              = "cn-shanghai"
    encrypt             = true
    tablestore_endpoint = "https://apihub-tflock.cn-shanghai.ots.aliyuncs.com"
    tablestore_table    = "tflock_prod"
  }
}
