## 变更说明

<!-- 一句话讲清楚改了什么、为什么 -->

## 变更类型

- [ ] feat      新功能
- [ ] fix       bug 修复
- [ ] refactor  重构（不改对外行为）
- [ ] docs      文档
- [ ] chore     构建 / CI / 依赖
- [ ] breaking  ⚠️ 破坏性变更（需标注迁移指南）

## 影响范围

- 影响服务：
- 影响租户：所有 / 部分（仅 `tenant_id=...`） / 不影响
- 影响接口契约：是 / 否
- 影响 DB schema：是（迁移文件：`migrations/xxx.sql`）/ 否

## 验证

- [ ] 本地 lint / 单测通过
- [ ] schema / manifests CI 通过
- [ ] 已本地手动验证（描述：xxx）
- [ ] 已更新文档（链接：xxx）

## 安全 / 合规自检（涉密/PII/审计）

- [ ] 无敏感数据进日志（按 masking 规则脱敏）
- [ ] 新接口已声明鉴权方式
- [ ] 变更操作已加 audit_log（如适用）
- [ ] 多租户：所有 DB 查询走 RLS（不写 tenant_id 条件）

## 关联

- 关联 Issue：#
- 关联 ADR：[00-decisions.md#](../docs/00-decisions.md)
