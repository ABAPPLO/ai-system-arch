"""admin-bff —— 后台聚合 API + 审计（合并 audit 服务）。

5 个职责：
  1. 聚合下游服务（tenant-svc / api-registry / auth）的查询
  2. 跨表 join / 复杂查询在 BFF 完成
  3. RBAC 权限校验（超管 / 租户 owner/admin）
  4. 自动审计所有 mutation（POST/PUT/DELETE）
  5. 审计日志查询/导出（等保 2.0 三级要求在线 ≥ 6 月）

合并 audit 的理由（roadmap §3.2）：audit 表 RLS 友好 + admin-bff 本来就要读它
做合规展示，独立服务反而要做一遍 RLS 跨租户聚合。降耦合 + 减部署。
"""
