"""tenant-svc —— 租户生命周期 / 成员关系 / 配额规则管理。

落实 docs/11-multi-tenant.md 的 §7 生命周期 + §8 服务定义 + docs/03 §3.14 API。

5 个职责：
  1. 租户 CRUD（仅超管；tenant 表无 RLS，全部走 admin_db_session）
  2. 状态机：active ↔ suspended / → closed
  3. 成员管理（owner/admin/developer/viewer 四角色）
  4. 配额规则（tenant.metadata.quota，PUT 触发 Redis 失效让 quota 服务重读）
  5. 缓存：t:{tenant_id}:meta 30min TTL，状态变更主动 DEL
"""
