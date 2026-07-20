# R3b 多 Region 全双活 — 设计 spec

> 日期：2026-07-19
> 阶段：APIHub fix-program · Wave 3 · R3b
> 关联：[2026-07-14 多 Region 全双活设计](2026-07-14-multi-region-active-active-design.md)、[fix-program spec](2026-07-15-apihub-fix-program-design.md) §3.7、ADR-013、[phase4 审计](../../phase4-audit-findings.md) §3.7
> base：main=`2ef6af6`（R3a #59 + Go quota 3 层 merge #60 合后）
> 依赖：R1c（APISIX 路由归属 #42）✓、R3a（Go quota region 字段 #59）✓

## 1. 目标与范围

兑现 ADR-013 多 Region 全双活的**代码/脚本/接缝**——审计 §3.7 点名「写亲和核心是空的」。本 spec 核实确认：多 Region 现有代码集中在单 commit `b209d50`，**8 个缺口全部成立**（详见 §3 现状）。R3b 把 5 个子系统全部做真，并在一个 kind 集群用「双实例」做 e2e 验证。

**范围决策（用户 2026-07-19 拍板）**：
- **全做**：S1 APISIX 写亲和 + S2 PG 双向逻辑订阅 + S3 Kafka MirrorMaker + S4 CH 跨区查询 + S5 failover runbook/drill，一个 squash-PR。
- **PG 复制模型**：全库双向 + `origin=none`（非 per-tenant 行过滤）。正确性承赖写分区。
- **验证拓扑**：单 kind 集群 + 双实例（2×PG / 2×Kafka+MM2 / 2×CH / 2×Redis / 2×dispatcher / 1×APISIX）。

**非目标（§9）**：per-tenant publication、GSLB 真云 DNS、真跨区分区/延迟、Redis key 加 region 段、真·季度 ops 演练、Phase 4 新服务多区部署、Terraform prod-bj infra（已存在）、Thanos 跨区监控（已做）。

## 2. 承重不变量（最关键）

**写分区（S1）是整面承重墙。** 全库双向复制无冲突的唯一前提是「每一行只在其 `home_region` 写」。因此 S1 必须覆盖**所有写**——不仅租户写（按 `tenant.home_region`），还包括 platform/admin 写（platform 归 `sh`）。任何绕过亲和检查的写路径都会制造双向写冲突。

**推论——交付顺序强依赖**：S1（写亲和）必须先落地并验证，S2（双向复制）才能被信任。任务排序（§7）据此。

## 3. 现状核实（8 个缺口，对照审计 §3.7，全部成立）

| # | 子系统 | 现状 | 证据 |
|---|---|---|---|
| 1 | APISIX 写亲和 | 端到端失效 | `tenant-affinity.lua` 从未投递进 pod（`apisix-values.yaml:45` 仅挂名无 volume）；`apisix_client.upsert_consumer`（`apisix_client.py:99-117`）body 写死无 home_region；`/internal/auth/check`（`auth/routes.py:111`）零调用者 |
| 2 | PG 逻辑订阅 | 脚本坏 | `setup-pg-logical-replication.sh:9/12` `$PG_DSN_SH/BJ` 未定义 + `set -u` 退；`:20` `FOR ALL TABLES`（对本模型正好正确）；`:28` `origin=none` 有；只建单向 |
| 3 | MirrorMaker | 脚本坏+无 k8s | `deploy-mirrormaker.sh:33/57` `$SH2BJ_TOPICS`/`$BIDIR_TOPICS` 未定义；`:7` `--whitelist` 弃用；docker run；`deploy/` 零 manifest |
| 4 | CH 跨区 | 零实现 | `config.py:57` `peer_region_ch_host` 声明 + prod-bj configmap 设了，业务零读取；`clickhouse.py` 单 client；`trace_svc/repository.py` 只 `FROM api_call_log` |
| 5 | quota splitRatio | 单区误砍 | Go `effectiveQuota = MaxCount × splitRatio`（`redis.go:178-180`），prod-sh configmap 设 `0.6` → 单区把配额砍到 60%；key 无 region 段（`redis.go:156-160`） |
| 6 | runbook | 半成品 | `failover-runbook.sh:26` `pg_promote` 真；`:45-46` DNS / `:41-43` Kafka = echo；`:15` lag 预检 `bc` 缺失静默跳过；`:14` 探针查幸存区非待提升区 |
| 7 | prod-sh overlay | 不对称 | `shared-infra-prod.yaml` 未设 `HOME_REGION=sh` / `PEER_REGION_CH_HOST`（靠默认/缺省） |
| 8 | PEER_REGION_PG_DSN | 字段缺失 | 设计里的逻辑订阅源 DSN，Python 侧无 Settings 字段 |

**已做实的**：`tenant.home_region` 列 + seed（`08-tenant-home-region.sql:3-10`）、prod-bj overlay/terraform/ArgoCD（`deploy/argocd/prod-bj.yaml`）、Go quota splitRatio 算法接线（部分，见 #5）、Thanos receive + 多区告警。
**PG 版本**：`docker-compose.dev.yml:37` `postgres:16-alpine` → `origin=none`（PG16+）可用，S2 成立。

## 4. 架构 / 数据流

```
client ──302(write,non-home)──► APISIX(home) ──► dispatcher(home) ──► PG(home, primary)
                                                                  │ bidir pub/sub + origin=none
                                                                  ▼
                                                          PG(peer, has all rows)
read  ──► 就近 dispatcher ──► 本地 PG/CH（写后读强主另行处理，§8-R2）
CH 全局/admin 查 ──► trace-svc remote(peer) UNION local
Kafka events ──► 本区 CH-writer；MM2 双向 + event_id 幂等去重
```

kind 双实例 harness：`pg-sh:5432` / `pg-bj:5433`、`kafka-sh` / `kafka-bj` + MM2 Deployment、`ch-sh` / `ch-bj`、`redis-sh` / `redis-bj`、2 dispatcher（`HOME_REGION=sh|bj`）、1 APISIX（`HOME_REGION=sh`）。**region 边界 = 进程/config 边界**，对代码验证足够；真网络分区/延迟不在 kind 范畴（§8-R5）。

## 5. 子系统设计

### S1 · APISIX 写亲和（端到端打通）

**设计决策**：
- home_region 走 APISIX consumer **`labels`**（原生支持、可索引），不碰非标 top-level 字段。`upsert_consumer` 加 `labels={"home_region": <t>}`；`tenant-affinity.lua:24` 改读 `ctx.consumer.labels.home_region`。
- 写亲和覆盖所有写：POST/PUT/PATCH/DELETE→写；GET/HEAD/OPTIONS→读就地。home_region 由 key 所属 tenant 决定（platform-admin key 属 platform tenant，seed `home_region=sh`），插件无需 special-case。
- 非 home 写返 `302 Location: ${PEER_REGION_GATEWAY}${request_uri}`（新增 env，每区指向对端网关）。home_region 缺失→**fail-open 写就地 + 结构化告警**（设计 §5.2 降级）。

**任务**：
- **S1-T1** `deploy/k8s/base/apigw/apisix-values.yaml`：ConfigMap 挂 `tenant-affinity.lua` + `extraVolumeMounts`→`/usr/local/apisix/apisix/plugins/` + config.yaml `plugins` 列表含 `tenant-affinity`。
- **S1-T2** `apisix_client.upsert_consumer`（`apisix_client.py:99-117`）加 `labels` 参数；`tenant-affinity.lua:24` 改读 `labels.home_region`（两端协同）。
- **S1-T3** `auth/routes.py:163 create_key` 查 tenant home_region（复用 `repository.py:64-74 get_tenant_home_region`）注入 consumer labels；`scripts/kind/apisix-setup.sh:240-242` smoke consumer 带 home_region。
- **S1-T4** `tenant-affinity.lua` 302 逻辑 + `PEER_REGION_GATEWAY` env + fail-open/告警。
- **S1-T5** `deploy/k8s/overlays/prod/shared-infra-prod.yaml` 补 `HOME_REGION=sh` + `PEER_REGION_GATEWAY` + `PEER_REGION_CH_HOST`（对称）。
- **S1-T6** 测试：Lua 302 决策矩阵单测 + 真 APISIX 集成测（投递插件 + consumer label → 断言 bj-home 写返 302 + Location）。

### S2 · PG 双向逻辑订阅（全库 + origin=none）

**设计决策**：
- 2 publication + 2 subscription 全库双向：`pub_all_sh` / `pub_all_bj`，对端 `CREATE SUBSCRIPTION sub_from_X_on_Y ... WITH (copy_data=true, origin=none)`（PG16 ✓）。幂等（DO block / drop-recreate，publication 无 IF NOT EXISTS）。
- 防回环靠 `origin=none`（非 per-tenant 行过滤）——正确性承赖 S1 写分区。
- lag 用 PG 侧 SQL 算（`pg_stat_subscription`），不依赖外部 `bc`。

**任务**：
- **S2-T1** 重写 `scripts/multi-region/setup-pg-logical-replication.sh`：param DSN（`PG_DSN_SH`/`PG_DSN_BJ` 来自 env）、双向、幂等、`wal_level=logical` + PG16 前置检查。
- **S2-T2** `config.py` 加 `peer_region_pg_dsn` Settings 字段。
- **S2-T3** lag 查询 helper（`scripts/multi-region/` 下，runbook + 监控共用）。
- **S2-T4** 双 PG e2e：写 sh→现 bj；写 bj→现 sh；**模拟回环行→断言不再复制回**（origin=none 核心断言）。

### S3 · Kafka MirrorMaker（k8s 化 + MM2）

**设计决策**：
- 弃 MM1 脚本，上 **MM2（MirrorMaker 2）k8s Deployment**：原生双向 + `IdentityReplicationPolicy` 防 topic 改名回环 + 内置 offset sync。allowlist：双向 `api-call-events`/`task-requests`/`task-failures`/`audit-events`/`billing-events`；`notification-events` 单向（设计 §4.3）。
- 幂等：MM2 防环 + 下游消费者按 R0b 契约 `event_id` 去重（核查 + 文档化）。
- manifest `deploy/k8s/base/shared/mirrormaker-deployment.yaml` + overlay patch broker 地址。

**任务**：
- **S3-T1** MM2 Deployment（双向 + IdentityReplicationPolicy + allowlist）；归档/删坏脚本 `deploy-mirrormaker.sh`。
- **S3-T2** overlay broker 地址（dev kind / prod / prod-bj）。
- **S3-T3** 消费者 `event_id` 去重核查 + 文档。
- **S3-T4** 双 Kafka + MM2 e2e：produce sh topic→consume bj；反向；**count 断言无重复**。

**S3-T3 event_id 去重核查（2026-07-19）**：

`grep -rn "event_id\|ON CONFLICT\|idempot" services/services/{executor,retry,trace,quota}/src` 结论：

- **R0b 契约实际未定义 `event_id` 字段**——`apihub_core/events.py` 的 4 个 typed 事件（`TaskRequest`/`TaskStatus`/`TaskFailure`/`CallEvent`）均无 `event_id`。下游去重靠业务主键，不是 `event_id`。
- **executor**：无 `event_id`；去重靠 `repository.mark_running` 原子 `pending→running`（`processor.py:4` 注释、`repository.py:14` 注释明示 at-least-once）+ 消费侧 commit-after-process（`consumer.py:5`）。重复投递命中 `UPDATE...WHERE status='pending'` 返回 0 → 跳过。
- **retry**：无 `event_id`；去重靠 `INSERT...ON CONFLICT DO NOTHING` 命中 partial unique index `idx_retry_task_active_dedup`（`UNIQUE(task_instance_id) WHERE status IN ('pending','running')`，`repository.py:45/65`）。活跃态重投静默跳过；dead/succeeded 后再失败可建新行。
- **trace**：**无任何去重**——CH-writer（Kafka→ClickHouse）无幂等保护，重复事件会写重复行。
- **quota**：无消费者去重——仅 `kafka.emit` 生产 `billing-events`（`routes.py:62`），无消费者。

**MM2 幂等约定**：MM2 `IdentityReplicationPolicy` 防 topic 改名回环（`sh.topic` 不会再被复制成 `bj.sh.topic`），但跨区消费仍 **at-least-once**——网络分区/MM2 重启都会产生重复投递。所有 Kafka 消费者须按事件业务主键去重：

- executor/retry 现状靠 `task_id` / `task_instance_id` + `ON CONFLICT` / 状态机原子转换已满足（不依赖 `event_id`）。
- **trace（CH-writer）是缺口**：MM2 双向复制后，同一 `CallEvent` 可能在 sh 和 bj 各消费一次 → ClickHouse 重复行。S3-T4 e2e 的「count 断言无重复」会暴露此缺口；follow-up 需给 `CallEvent` 加 `event_id`（或用 `request_id`+`trace_id` 复合键）+ CH ReplacingMergeTree/幂等写入。列为 §9 follow-up（非本轮）。

### S4 · CH 跨区查询（trace-svc remote()）

**设计决策**：
- `clickhouse.py` 加 lazy `peer_client`（`settings.peer_region_ch_host`，None→单区安全）+ `query_global()`：`remote($peer,...) UNION ALL local`。
- trace-svc **admin/全局聚合查询**走 union；per-tenant/region-local 不变。
- 加 `peer_region_ch_user/password` Settings + 双区 configmap。

**任务**：
- **S4-T1** `clickhouse.py` peer_client + `remote()`/union helper（guarded by config presence）。
- **S4-T2** Settings `peer_region_ch_user/password` + 双区 configmap（prod-bj 补 creds，prod-sh 补 `ch-bj.internal`）。
- **S4-T3** `trace_svc/repository.py` 全局查询接 union；local 查询不动。
- **S4-T4** 双 CH e2e：local 写一行 + peer 写一行→全局查 UNION 返两条；peer 未配→仅 local。

### S5 · Failover runbook + drill

**设计决策**：
- 修静默 bypass：lag 比较改 PG 侧 SQL（`SELECT CASE WHEN replay_lag > interval '5s' THEN 1 ELSE 0 END`），不依赖 `bc`。
- 修探针：failover sh→bj 前，查 bj 的 `pg_stat_subscription`（from-sh 订阅）lag，要求追平；sh 已挂则 `--force`。
- DNS 真调用：包 `aliyun alidns` CLI（kind/dev `--dry-run` 跳过 + 告警，非纯 echo）；Kafka CG reset 真调 `kafka-consumer-groups --reset-offsets`；MM2 方向反转 = 缩 sh→bj 副本至 0。
- 每阶段写 `audit_log`（等保，接 R0a 审计）。
- drill = `scripts/multi-region/drill-failover.sh`：对 kind 双实例注入故障（缩 sh dispatcher 至 0 / 断 PG）→ 跑 runbook 真 `pg_promote` → 断言读写在提升后的 bj 落地 → rollback。即「季度演练」的**可重复自动化 harness** 交付物。

**任务**：
- **S5-T1** `failover-runbook.sh` 修 bc bypass + 探针目标（PG 侧 lag SQL）。
- **S5-T2** DNS 真 aliyun 调用（kind skip+告警）+ Kafka CG reset 真 + MM2 方向反转。
- **S5-T3** runbook 各阶段 `audit_log`。
- **S5-T4** `drill-failover.sh` harness（注入→runbook→assert→rollback）对 kind 双区。

### 跨切面（C）

- **C1** Go quota splitRatio 单区守卫：`PEER_REGION_*` 任一未配置 → `splitRatio` 强制 1.0（无视 `QUOTA_REGION_SPLIT_RATIO` env）；仅 peer 配置齐备时才按比例——修「单区砍 60%」潜伏 bug（`redis.go:178-180 effectiveQuota` + `main.go:50` wiring）。代码层守卫覆盖已部署 prod-sh 的 0.6 configmap，不依赖人工改。
- **C2** prod-sh overlay 对称：`HOME_REGION=sh` / `PEER_REGION_CH_HOST`(+creds) / `PEER_REGION_GATEWAY`（S1-T5 / S4-T2 共用）。
- **C3** `peer_region_pg_dsn` Settings（= S2-T2）。
- **C4** per-region Redis 强制：Redis key **不加** region 段（保持与 Python + R3a/#60 对齐），靠「每区独立 Redis」隔离；kind 双实例给两区 quota 各自独立 Redis，否则计数相撞。设计 §4.2 的 region 前缀列 future hardening，本轮不做（避免再次撕裂 Go/Python key 对齐）。

## 6. 验证总表（每条从真入口驱动，对照审计 §6）

| 子系统 | 真入口（非中段注入） | 断言 |
|---|---|---|
| S1 插件 | httpx POST 真 APISIX（consumer label home_region=bj），`follow_redirects=False` | 302 + Location 指向 peer gateway |
| S1 决策 | Lua 单测（mock consumer/region/method 全分支） | GET 放行 / POST non-home→302 / POST home 放行 / 无 home_region→fail-open |
| S2 复制 | 直写 sh PG 一行 → 查 bj PG | bj 出现；反向亦然；**回环行不再复制** |
| S3 MM2 | produce 到 sh kafka → consume 自 bj | bj 收到；反向；count 无重复 |
| S4 CH | trace-svc 全局查询（双 CH 各写一行） | UNION 返两条；peer 未配返一条 |
| S5 runbook | drill：缩 sh dispatcher 至 0 → 跑 runbook → 查 bj | bj 提升后读写真落地 |
| C1 splitRatio | Go quota check（peer 未配） | admitted 上限 = rule MaxCount（非 60%） |

每条异步/复制链路都从真入口驱动，非 smoke 中段注入（§6 系统性盲区）。drill 故障注入（缩 deployment）是真 ops 动作。

## 7. 任务依赖与排序

```
S1-T1..T6（写亲和）─┐
                   ├─► S2-T1..T4（双向复制，承赖 S1 写分区）─┐
                   │                                          ├─► S5-T4 drill（需 S1+S2+S3 数据面就绪）
S3-T1..T4（MM2）───┤                                          │
S4-T1..T4（CH）────┘                                          │
C1/C2/C3/C4（跨切面）可与 S1..S4 并行 ─────────────────────────┘
S5-T1..T3（runbook 修复）可与上并行；S5-T4 drill 收尾
```

S1 先行；S2 必须在 S1 验证后；S5-T4 drill 是最后集成关。

## 8. 风险

- **R1 origin=none 正确性全靠 S1 写分区**：绕过亲和的写→双向冲突。缓解：S1 先落地验证（§7 排序）+ 可选双向冲突巡检（比两区同 row `updated_at` 差异告警，列 follow-up）。
- **R2 复制延迟 read-your-writes**：写 home→就近读非 home 可能旧值。本轮接受 lag + `>30s` 告警；高危读强主列后续。
- **R3 MM2 运维复杂度**：配置错静默丢消息。缓解：e2e count 断言 + 谨慎 `sync_group_offsets`；schema-registry 列 follow-up（§9-D）。
- **R4 CH remote() 跨区延迟**：仅 admin/全局用，超时 + 本地优先降级。
- **R5 drill 无法模拟真分区/延迟**（kind 单宿主）：明确 drill 验「切换逻辑+数据链路」；分区行为靠 staging/prod 真演练。
- **R6 runbook DNS/Kafka 在 kind 跳过**：分支 mock 单测；首次 staging 演练强制覆盖。
- **R7 splitRatio 行为变更**：代码层守卫覆盖 prod-sh 0.6 configmap（C1）。

## 9. Out-of-scope（本轮不做）

- per-tenant publication（PG15 行过滤，本轮全库双向替代）
- GSLB 真云解析 DNS / 真跨区分区延迟（阿里云 infra + staging/prod）
- Redis key 加 region 段（保持 Go/Python 对齐，靠独立 Redis 隔离）
- 真·季度 ops 演练（本轮交付可重复 harness）
- Phase 4 新服务（notification/ai-gateway/billing/portal）多区部署（prod-bj overlay 现与 prod 同列 11 服务；新服务多区后续）
- Terraform prod-bj infra（已存在）/ Thanos 跨区监控（已做）

## 10. 参考

- [2026-07-14 多 Region 全双活设计](2026-07-14-multi-region-active-active-design.md)
- [fix-program spec](2026-07-15-apihub-fix-program-design.md) §3.7 / §5 Wave 3
- [phase4 审计](../../phase4-audit-findings.md) §3.7 / §9-F
- ADR-013（`docs/00-decisions.md`）
- 关联记忆：[[apihub-fix-program-progress]] [[apihub-audit-2026-07-15]] [[k8s-kind-cluster-env]]
