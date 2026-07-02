output "vpc_id" {
  value = alicloud_vpc.this.id
}

output "vpc_cidr" {
  value = alicloud_vpc.this.cidr_block
}

output "dmz_vswitch_ids" {
  value = alicloud_vswitch.dmz[*].id
}

output "app_vswitch_ids" {
  value = alicloud_vswitch.app[*].id
}

output "data_vswitch_ids" {
  value = alicloud_vswitch.data[*].id
}

output "mgmt_vswitch_ids" {
  value = alicloud_vswitch.mgmt[*].id
}

output "security_group_ids" {
  value = {
    dmz  = alicloud_security_group.dmz.id
    app  = alicloud_security_group.app.id
    data = alicloud_security_group.data.id
    mgmt = alicloud_security_group.mgmt.id
  }
}
