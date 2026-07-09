# 远程 backend：OSS + TableStore（锁）
# 首次需先手动创建 OSS bucket + TableStore 实例
# 详见 docs/09-deployment.md §5.2

terraform {
  backend "oss" {
    bucket              = "apihub-tfstate-dev"
    prefix              = "terraform/dev"
    region              = "cn-shanghai"
    encrypt             = true
    tablestore_endpoint = "https://apihub-tflock.cn-shanghai.ots.aliyuncs.com"
    tablestore_table    = "tflock_dev"
  }
}
