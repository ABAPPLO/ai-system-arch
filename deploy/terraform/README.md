# Terraform IaC

> 落实 [ADR-001 阿里云](../../docs/00-decisions.md#adr-001-云厂商)、[ADR-008 单 Region](../../docs/00-decisions.md#adr-008-多-region-策略)、[ADR-010 等保 2.0 三级](../../docs/00-decisions.md#adr-010-数据合规)。

## 目录

```
terraform/
├── versions.tf                # provider 版本锁定
├── modules/                   # 可复用模块
│   ├── vpc/                   # VPC + 4 子网 + 4 安全组（DMZ/App/Data/Mgmt）
│   ├── ack/                   # ACK 托管版集群
│   ├── rds/                   # PostgreSQL 主备 + SQL 审计
│   ├── redis/                 # Redis 集群
│   ├── kafka/                 # Kafka 实例 + 初始 topic
│   └── oss/                   # OSS（SSE-KMS + 版本管理）
└── envs/
    ├── dev/                   # 开发环境
    │   ├── main.tf            # 组合 6 个模块
    │   ├── variables.tf
    │   ├── outputs.tf
    │   ├── backend.tf         # 远程 state（OSS + TableStore 锁）
    │   └── terraform.tfvars.example
    ├── staging/               # TODO
    └── prod/                  # TODO
```

## 使用流程

```bash
# 1. 准备凭据（环境变量，避免硬编码）
export ALICLOUD_ACCESS_KEY="LTAI..."
export ALICLOUD_SECRET_KEY="..."
export TF_VAR_rds_password='<强密码>'

# 2. 首次需手动创建 OSS bucket（tfstate）和 TableStore 表（state 锁）
#    - apihub-tfstate-dev
#    - apihub-tflock（TableStore，主键 LockID）

# 3. 进入 dev 环境
cd envs/dev

# 4. 初始化（拉取 provider + 配 backend）
terraform init

# 5. 看变更
terraform plan

# 6. 应用
terraform apply

# 7. 拿 kubeconfig
terraform output -raw kubeconfig > ~/.kube/apihub-dev
export KUBECONFIG=~/.kube/apihub-dev

# 8. 销毁（dev 慎用，prod 禁用）
# terraform destroy
```

## 关键约定

- **dev/staging/prod 严格物理隔离**：独立 VPC、独立 RDS、独立 K8s 集群。详见 [09-deployment §3.1](../../docs/09-deployment.md#31-三套环境对照)。
- **prod 不开 delete_protection=false**：误删保护强制打开。
- **RDS 强制 SSL**：参数 `rds.force_ssl=on`。
- **OSS 强制 SSE-KMS**：所有对象服务端 KMS 加密。
- **密码禁硬编码**：通过 `TF_VAR_xxx` 环境变量或 KMS + Sealed Secrets 注入。
- **state 远程托管**：OSS + TableStore 锁，禁止本地 state。

## 模块参数速查

| 模块 | 关键变量 | 默认值（dev） |
|------|---------|--------------|
| vpc | vpc_cidr | 10.0.0.0/16 |
|     | availability_zones | shanghai e/f/g |
| ack | node_count | 5 |
|     | node_instance_type | ecs.c7.2xlarge |
| rds | instance_type | pg.n2.medium.2c |
|     | storage | 100 GB |
| redis | instance_class | redis.master.small.default |
| kafka | topics | 6 个默认 topic |
| oss  | bucket_name | apihub-{env}-objects |

## 添加新资源原则

1. 模块化优先：能复用的就写模块
2. 加敏感输出：passwords / tokens 一律 `sensitive = true`
3. 加 tags / labels：所有资源打 `Environment` 标签
4. 加 deletion_protection：prod 必须 `true`
5. PR 评审：plan 输出附 PR

## 状态管理

- **远程 state**：`oss::apihub-tfstate-{env}`
- **state 锁**：TableStore `tflock_{env}`
- **严禁**手改云资源 → 一定要走 Terraform
- **严禁**手改 state → 用 `terraform state` 命令
- **state 备份**：OSS 自带版本管理 + 跨 Region 复制
