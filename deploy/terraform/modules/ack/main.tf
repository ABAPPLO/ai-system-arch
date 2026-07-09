# ACK 托管版 — 详见 docs/09-deployment.md §2

resource "alicloud_cs_managed_kubernetes" "this" {
  name                         = "apihub-${var.environment}-ack"
  cluster_spec                 = "ack.pro.small"
  worker_vswitch_ids           = var.vswitch_ids
  pod_vswitch_ids              = var.vswitch_ids
  security_group_id            = var.security_group_id
  is_enterprise_security_group = true
  new_nat_gateway              = false
  # provider 仅有 deletion_protection（集群级删除保护）；原 worker_deletion_protection 无对应 arg，
  # 已并入 deletion_protection（prod 开启）。
  deletion_protection = var.environment == "prod" ? true : false
}

# 默认节点池（system + compute 共用，后续按需拆分）
resource "alicloud_cs_kubernetes_node_pool" "default" {
  cluster_id           = alicloud_cs_managed_kubernetes.this.id
  name                 = "apihub-${var.environment}-compute"
  vswitch_ids          = var.vswitch_ids
  instance_types       = [var.node_instance_type]
  desired_size         = var.node_count
  system_disk_size     = var.node_disk_size
  system_disk_category = var.node_disk_category
  password             = data.external.random_password.result.password
}

data "external" "random_password" {
  program = ["bash", "-c", "echo '{\"password\":\"'$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24)'\"}'"]
}
