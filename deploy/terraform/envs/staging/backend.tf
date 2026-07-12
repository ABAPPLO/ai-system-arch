# 远程 backend：OSS + TableStore（锁）—— staging 独立 state，与 dev/prod 隔离
# 首次需先手动创建 OSS bucket（apihub-tfstate-staging）+ TableStore 表（tflock_staging）
# 详见 docs/09-deployment.md §5.2

terraform {
  backend "oss" {
    bucket              = "apihub-tfstate-staging"
    prefix              = "terraform/staging"
    region              = "cn-shanghai"
    encrypt             = true
    tablestore_endpoint = "https://apihub-tflock.cn-shanghai.ots.aliyuncs.com"
    tablestore_table    = "tflock_staging"
  }
}
