output "cluster_id" {
  value = alicloud_cs_managed_kubernetes.this.id
}

output "cluster_name" {
  value = alicloud_cs_managed_kubernetes.this.name
}

output "api_server_endpoint" {
  value = alicloud_cs_managed_kubernetes.this.connections["api_server_internet_endpoint"]
}

output "kubeconfig" {
  value     = alicloud_cs_managed_kubernetes.this.kube_config
  sensitive = true
}
