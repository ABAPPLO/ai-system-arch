# 阿里云 Kafka（消息队列 Kafka 版，兼容开源）

resource "alicloud_alikafka_instance" "this" {
  name           = "apihub-${var.environment}-kafka"
  partition_num  = 50
  disk_type      = 1
  disk_size      = var.disk_size
  deploy_type    = 4
  io_max_spec    = "alikafka.hw.2xlarge"
  spec_type      = "professional"
  vswitch_id     = var.vswitch_id
  security_group = var.security_group_id
}

resource "alicloud_alikafka_topic" "this" {
  count         = length(var.topics)
  instance_id   = alicloud_alikafka_instance.this.id
  topic         = var.topics[count.index].name
  partition_num = var.topics[count.index].partition_count
  remark        = "retention=${var.topics[count.index].retention_hours}h"
}
