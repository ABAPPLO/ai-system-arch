# 00 · 架构决策记录（ADR）

> 本文记录 APIHub 项目设计评审（2026-07-02）中锁定的 12 个关键架构决策。
>
> 每条决策包含：上下文、决策、备选方案、影响。后续如需变更，需走"决策变更流程"。

## 决策一览

| ADR | 主题 | 状态 |
|-----|------|------|
| [ADR-001](#adr-001-云厂商) | 云厂商：阿里云 | ✅ Accepted |
| [ADR-002](#adr-002-商业化策略) | 商业化：内免外收 | ✅ Accepted |
| [ADR-003](#adr-003-接口接入方式) | 接入方式：双轨并行（UI + YAML） | ✅ Accepted |
| [ADR-004](#adr-004-ai-网关扩展) | AI 网关：现在预留扩展点 | ✅ Accepted |
| [ADR-005](#adr-005-审批流强度) | 审批流：分级审批 | ✅ Accepted |
| [ADR-006](#adr-006-api-key-轮换) | Key 轮换：推荐但不强制 | ✅ Accepted |
| [ADR-007](#adr-007-im-集成) | 审批 / 通知 IM：钉钉 | ✅ Accepted |
| [ADR-008](#adr-008-多-region-策略) | 多 Region：单 Region 长期 | 🔄 Superseded by ADR-013 |
| [ADR-013](#adr-013-多-region-全双活) | 多 Region：租户亲和全双活 | ✅ Accepted |
| [ADR-009](#adr-009-多租户策略) | 多租户：平台多租户 | ✅ Accepted |
| [ADR-010](#adr-010-数据合规) | 合规：等保 2.0 三级 | ✅ Accepted |
| [ADR-011](#adr-011-实名认证) | 实名：邮箱 + 手机号 | ✅ Accepted |
| [ADR-012](#adr-012-对外开放时间) | 开放时间：Phase 3 内（~M11） | ✅ Accepted |

---

## ADR-001 云厂商

**Status**: Accepted · **Date**: 2026-07-02

### 上下文
平台部署在单云。需选一家云厂商，决定 Terraform module 写法、托管服务选型、成本估算。

### 决策
**阿里云**。

### 备选方案
| 方案 | 优势 | 劣势 |
|------|------|------|
| ✅ 阿里云 | 国内生态最全，ACK/RDS PG/Redis 集群/Kafka/OSS 托管成熟 | ClickHouse / Jaeger / Loki 仍需自建 |
| ❌ 腾讯云 | TKE/CDB/CKafka 对标，价格略低 | 互联网生态略弱于阿里云 |
| ❌ 华为云 | 政企客户偏好，自主可控 | 生态略弱 |
| ❌ AWS 中国 | 国际化友好 | 国内使用受限，成本较高 |

### 影响
- Terraform 用 `aliyun/alicloud` provider
- 托管服务：ACK 托管版、RDS PG 企业版、Redis 7.0 集群版、消息队列 Kafka、OSS、SLB、云解析、CDN、WAF
- 自建：ClickHouse（ECS 集群）、Jaeger、Loki、Prometheus
- 月成本预估 ~¥117,000（详见 09-deployment.md）

---

## ADR-002 商业化策略

**Status**: Accepted · **Date**: 2026-07-02

### 上下文
平台既要服务内部业务线，也要对外开放给外部开发者。需决定是否计费、计费范围。

### 决策
**内免外收**：
- 内部业务线 / 子公司之间调用免费
- 外部开发者按调用计费，覆盖平台成本

### 备选方案
| 方案 | 优势 | 劣势 |
|------|------|------|
| ❌ 完全免费 | 最简单 | 无经济手段调节外部滥用 |
| ✅ 内免外收 | 内部不结算，外部商业化 | billing 模块仍需开发 |
| ❌ 内外都计费 | 完整财政治理 | 部门间结算复杂 |

### 影响
- Phase 1/2：quota 服务仅做配额（不做计费）
- Phase 3 启用：新增 `billing_account` / `billing_record` / `subscription` 三张表
- 数据模型在 04-data-model.md 中标注 Phase 3 表
- 计费规则：免费配额 + 计次 + 包月（详见后续 dedicated 文档）

---

## ADR-003 接口接入方式

**Status**: Accepted · **Date**: 2026-07-02

### 上下文
接口提供方如何把接口注册到平台？不同团队工程化程度不同。

### 决策
**双轨并行**：
- UI 接入：后台可视化操作，简单上手
- 声明式 YAML：Git + PR + CI，工程化强约束

两者底层共享元数据模型，互相同步。

### 备选方案
| 方案 | 优势 | 劣势 |
|------|------|------|
| ❌ 仅 UI | 上手最快 | 版本管理弱 |
| ❌ 仅 YAML | GitOps 友好 | 学习成本高，非工程角色难参与 |
| ✅ 双轨并行 | 覆盖所有团队 | 开发成本高（两套都要做） |

### 影响
- api-registry 同时提供 HTTP API（UI 用）和 GitOps reconciler（YAML 用）
- YAML 仓库：`apihub-apis` monorepo，每个接口一个 YAML
- UI 修改也写入底层 PG，YAML reconciler 检测到反向同步到 Git（可选）
- 接口元数据有 `source` 字段：`ui` / `yaml` / `migration`

---

## ADR-004 AI 网关扩展

**Status**: Accepted · **Date**: 2026-07-02

### 上下文
选 Python 作为业务语言的核心理由之一是 AI 生态。需决定是否在初期就为 AI 网关预留扩展点。

### 决策
**现在就预留**：
- 接口元数据加 `backend.type=ai_model` 枚举值
- 调用日志加 `token_usage` 字段
- 配额加 `token_quota` 维度
- 文档生成支持流式响应（SSE）
- Phase 4 实现 LLM 推理路由

### 备选方案
| 方案 | 优势 | 劣势 |
|------|------|------|
| ✅ 现在预留 | 零额外成本，未来扩展平滑 | 设计阶段需要更多考虑 |
| ❌ 暂不预留 | 聚焦当前需求 | Phase 4 改造工作量大（涉及 schema、日志、配额、文档多个模块） |

### 影响
- 04-data-model.md：`api_version.backend_config.type` 支持 `http/grpc/script/mq/ai_model`
- 04-data-model.md：`api_call_log` 加 `token_prompt / token_completion / token_total` 字段
- 04-data-model.md：quota 表加 `token_quota_daily / token_quota_monthly`
- 06-high-concurrency.md：增加 SSE / 流式响应处理章节
- 07-developer-portal.md：文档生成支持流式响应示例

---

## ADR-005 审批流强度

**Status**: Accepted · **Date**: 2026-07-02

### 上下文
接口发布、版本变更、授权申请等操作是否需要审批？

### 决策
**分级审批**：
- dev 环境：自助发布（接口提供方决定）
- staging 环境：简单审批（业务负责人一次确认）
- prod 环境：强审批（平台运维评审 + 灰度策略）

### 备选方案
| 方案 | 优势 | 劣势 |
|------|------|------|
| ❌ 全部强审批 | 最安全 | MVP 阶段成瓶颈 |
| ✅ 分级审批 | 兼顾效率与安全 | 权限模型稍复杂 |
| ❌ 全自助 | 最快 | 事故风险高 |

### 影响
- 03-services.md：api-registry 加 `change_request` 工单子系统
- 04-data-model.md：已有 `api_change_request` 表，按环境分级状态机
- 05-core-flows.md：发布流程图按环境分三档
- 钉钉审批流：prod 发布走钉钉审批（见 ADR-007）

---

## ADR-006 API Key 轮换

**Status**: Accepted · **Date**: 2026-07-02

### 决策
**推荐但不强制**：
- UI 提示 90 天轮换（提醒但不强制）
- 外部 Key 默认 1 年过期（可续期）
- 内部 Key 不过期（记录 `last_rotated_at`）

### 备选方案
| 方案 | 优势 | 劣势 |
|------|------|------|
| ❌ 强制 90 天 | 最高安全 | 调用方体验差 |
| ❌ 强制 1 年 | 中等 | 仍有打扰 |
| ✅ 推荐不强制 | 平衡 | 安全依赖调用方自觉 |
| ❌ 不限制 | 最简单 | 外部场景不安全 |

### 影响
- auth 服务：API Key 创建时记录 `expires_at` + `last_rotated_at`
- 调用方门户展示轮换提示
- 过期前 30 天 / 7 天自动邮件提醒
- 内部 Key 不强制，但 `last_rotated_at > 180 天` 在后台标红

---

## ADR-007 IM 集成

**Status**: Accepted · **Date**: 2026-07-02

### 决策
**钉钉**：接口发布 / 授权申请 / 配额预警 / 异常告警走钉钉。

### 备选方案
| 方案 | 备注 |
|------|------|
| ✅ 钉钉 | 阿里云生态近，公司选用 |
| ❌ 飞书 | 团队不用 |
| ❌ 企业微信 | 团队不用 |
| ❌ 自建工单 | 开发量大，无优势 |

### 影响
- 03-services.md：加 `notification-svc` 服务，封装钉钉开放平台
- 钉钉能力：审批流（接口发布 / 授权）、群机器人（告警）、工作通知（用户级）
- 钉钉身份与平台账号绑定：通过 OAuth 或邮箱匹配
- 国际化场景预留：notification-svc 抽象接口，可后续支持其他 IM

---

## ADR-008 多 Region 策略

**Status**: 🔄 Superseded by [ADR-013](#adr-013-多-region-全双活) · **Date**: 2026-07-02 · **Superseded**: 2026-07-14

> ⚠️ 本决策已被 ADR-013 取代。APIHub 现采用租户亲和全双活架构（cn-shanghai + cn-beijing），而非单 Region 长期策略。

### 决策
**单 Region 长期**：
- 主 Region 多 AZ 高可用（3 AZ 均匀分布）
- 跨 Region 仅做数据备份（PG binlog 备份 + OSS 跨区域复制）

### 备选方案
| 方案 | RTO | 成本 |
|------|-----|------|
| ✅ 单 Region | 30min | 1x |
| ❌ 跨 Region 备份 | 30min | 1.3x |
| ❌ 跨 Region 主备 | 10min | 1.6x |
| ❌ 双活 | < 1min | 2x+ |

### 影响
- 仅在单 Region 部署 ACK / RDS / Redis / Kafka / ClickHouse
- 备份目标 Region：选阿里云另一 Region（如 cn-beijing）
- 后续如业务有更强可用性需求，可升级到"跨 Region 备份"
- ClickHouse 副本仍在同 Region 跨 AZ，避免跨 Region 复制延迟

---

## ADR-009 多租户策略

**Status**: Accepted · **Date**: 2026-07-02 · **重要**

### 决策
**平台多租户**：
- 公司各业务线 / 子公司作为独立租户
- 数据隔离 + 配额独立 + 报表独立
- 所有元数据表加 `tenant_id` 字段
- 应用层 + RLS 双重隔离

### 备选方案
| 方案 | 优势 | 劣势 |
|------|------|------|
| ❌ 单租户 | 最简单 | 业务线 / 子公司数据混在一起，配额无法独立 |
| ✅ 平台多租户 | 数据隔离、独立配额 | 所有查询要带 tenant_id，工作量大 |
| ❌ 商业多租户 | SaaS 卖给外部 | 当前无此业务，过度设计 |

### 影响 ⚠️
- **影响范围最大**：详见 [11-multi-tenant.md](11-multi-tenant.md)
- 所有元数据表加 `tenant_id` 字段
- 所有查询、API、后台 UI 加租户上下文
- Redis Key 加 tenant 前缀
- ClickHouse 调用日志加 tenant_id，物化视图按租户分组
- Kafka 消息 Header 带 tenant_id
- 配额、计费、调用日志、审计、文档可见性都按租户拆分
- 新增 `tenant-svc` 服务管理租户生命周期

---

## ADR-010 数据合规

**Status**: Accepted · **Date**: 2026-07-02

### 决策
**等保 2.0 三级**。

### 备选方案
| 方案 | 适用场景 |
|------|---------|
| ✅ 等保 2.0 三级 | 国内主流企业要求 |
| ❌ 等保 + GDPR | 有欧盟业务 |
| ❌ PCI-DSS | 涉及支付卡 |
| ❌ 基础要求 | 仅做基础安全 |

### 影响
- 08-observability-security.md 增加详细合规清单：
  - 审计日志保留 ≥ 6 个月（在线 + 归档）
  - 数据传输 + 存储加密（TLS 1.3 + KMS）
  - 访问控制（RBAC + MFA）
  - 定期备份 + 恢复演练
  - 漏洞管理 + 渗透测试
- 09-deployment.md 网络隔离加强：
  - 独立 Mgmt VPC + 堡垒机
  - 数据库审计（RDS PG SQL 审计）
  - 安全中心接入

---

## ADR-011 实名认证

**Status**: Accepted · **Date**: 2026-07-02

### 决策
**邮箱 + 手机号**：默认低门槛，按 API 敏感度可升级要求。

### 备选方案
| 方案 | 门槛 |
|------|------|
| ✅ 邮箱 + 手机号 | 低，快速接入 |
| ❌ 身份证 + 营业执照 | 中，需 OCR + 人工审核 |
| ❌ 仅营业执照 | 仅企业可接入 |
| ❌ 按敏感度分级 | 灵活但运营复杂 |

### 影响
- Portal 注册流程：邮箱验证 + 手机短信验证
- 用户表 `user` 加 `phone_verified` / `email_verified` 字段
- API 元数据可标 `min_verification_level`（默认 `basic`，金融 / 敏感 API 标 `enterprise`）
- 调用方调用敏感 API 时，平台检查 `app.verification_level ≥ api.min_verification_level`

---

## ADR-012 对外开放时间

**Status**: Accepted · **Date**: 2026-07-02

### 决策
**Phase 3 内（~M11）对外开放**。

### 备选方案
| 方案 | 时间 |
|------|------|
| ✅ Phase 3 内（~M11） | 按路线图推进 |
| ❌ 加速（~M9） | 提前但削减功能 |
| ❌ 推迟（~M12+） | 内部跑顺再开放 |
| ❌ 暂不开放 | 仅内部用 |

### 影响
- Portal / SDK / 计费按 M11 节奏投入
- Phase 3（M9-M11）核心任务：Portal 前端、portal-bff、SDK 生成、沙箱、配额计费、Webhook
- M10 月底：Portal Beta，邀请 3-5 个外部调用方
- M11 月底：对外开放

---

## 待决策项

| # | 决策 | 状态 |
|---|------|------|
| A | MVP 试点业务（哪 2-3 个业务线先接入） | ⏳ 待用户提供 |
| B | 年度预算批准 | ⏳ 流程性 |

---

## ADR-013 多 Region 全双活

**Status**: Accepted · **Date**: 2026-07-14 · **supersedes**: ADR-008

### 上下文
APIHub 业务增长，单 Region 无法满足 P0 级可用性要求。需要在不引入 CRDT/TiDB 的前提下，实现跨 Region 高可用。

### 决策
**租户亲和 + 写分区 + 读双活**：
- Region：cn-shanghai + cn-beijing，通过阿里云云解析 GSLB 就近接入
- 写分区：每个租户固定 `home_region`（`sh`/`bj`），写请求 302 跳转到 home_region
- 读双活：任一 Region 均可处理读请求
- PG 同步：双向逻辑订阅，`origin = none` 防循环，按 tenant 拆 publication
- Redis：双 Region 独立 Cluster，配额按 `QUOTA_REGION_SPLIT_RATIO` 比例分配
- Kafka：MirrorMaker 2 双向复制
- ClickHouse：双 Region 独立集群，跨 Region 查询通过 `PEER_REGION_CH_HOST` 配置
- 监控：双 Region Prometheus remote_write → Thanos Receiver 统一视图
- 切换：人工确认 + 半自动 runbook
- 演练：每季度一次故障切换演练

### 备选方案
| 方案 | 优势 | 劣势 |
|------|------|------|
| ✅ 租户亲和 + 写分区 | 实现简单，无数据冲突 | 故障时需迁移租户 home_region |
| ❌ CRDT 多主写入 | 无冲突 | 需 TiDB/CockroachDB，改造成本高 |
| ❌ 只读副本 | 实现简单 | 写故障不解决 |

### 影响
- 04-data-model.md：tenant 表加 `home_region` 字段
- 09-deployment.md：新增 prod-bj 环境
- 跨 Region 新增 VPC Peering / 逻辑复制 / MirrorMaker / CH 配置
- 新增 cost：~¥58,300/月（详见设计文档）

---

## 延后决策项

| # | 决策 | 触发时机 |
|---|------|---------|
| C | 是否 Go 重写热点服务（如 quota） | Phase 4 性能瓶颈出现时 |
| E | 是否引入商业多租户（卖给外部 SaaS） | 业务方向变化时 |

---

## 决策变更流程

如需变更已 Accepted 的决策：

1. 提出者在 docs/00-decisions.md 顶部添加"变更申请"段落
2. 说明：变更原因、新方案、影响面、迁移计划
3. 评审会决议
4. 通过后：
   - 原 ADR 状态改为 `Superseded by ADR-XXX`
   - 新建 ADR 接替
   - 更新受影响的所有文档

禁止直接修改已 Accepted 的 ADR 内容。
