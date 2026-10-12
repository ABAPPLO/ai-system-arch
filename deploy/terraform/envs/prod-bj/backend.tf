# 远程 backend：OSS + TableStore（锁）—— prod-bj 独立 state，与 prod 隔离
# 首次需先手动创建 OSS bucket（apihub-tfstate-bj）+ 北京 Region TableStore 实例
# （apihub-tflock-bj）及表（tflock_prod_bj）——详见 docs/09-deployment.md §5.2
# ⚠️ 无 tablestore 锁时并发 terraform apply 可损坏 state（其余环境均已配锁）。

terraform {
  backend "oss" {
    bucket              = "apihub-tfstate-bj"
    prefix              = "prod-bj"
    key                 = "terraform.tfstate"
    region              = "cn-beijing"
    encrypt             = true
    acl                 = "private"
    tablestore_endpoint = "https://apihub-tflock-bj.cn-beijing.ots.aliyuncs.com"
    tablestore_table    = "tflock_prod_bj"
  }
}
