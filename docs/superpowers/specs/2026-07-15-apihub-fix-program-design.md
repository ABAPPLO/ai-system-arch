# APIHub 修复 Program 设计（Fix Program Design）

日期：2026-07-15
状态：Draft（待 review）
依据：[phase4-audit-findings.md](../../phase4-audit-findings.md)（P0/P1/P2 实现层 + §9 架构层）
范围决策：**P0 + P1 + §9 架构缺陷 + Go quota 真替代 Python + 多 Region 兑现 ADR-013 全双活**（用户 2026-07-15 拍板，三项均取最大范围）。

> 本文档是 **program 级分解**，不是单个实现 spec。每个轮次（R0a…）是一个独立子项目，走自己的 spec→plan→implement→handoff，合并为一个 squash-PR。本文档定顺序、依赖、验收；R0a 的详细实现 spec 见 §6，其余轮次在启动时各自细化。

---

## 1. 背景与目标

审计结论：Phase 1-2 骨架真实可用；Phase 4 是"单服务代码写了、接缝处全没接上"。本 program 的目标不是堆新功能，而是：

1. **救活断裂链路**：retry 自动/手动重试、dispatcher→trace、api-registry→APISIX 发布闭环。
2. **让 Phase 4 能上线**：四服务部署 + 兑现关键 stub（notification / ai-gateway / GDPR）。
3. **根治架构缺陷**（§9）：路由归属、服务边界、Kafka 契约、CH 多租户护栏——否则同类 bug 持续再生。
4. **兑现两个大块**：Go quota 真替代 Python（含多 Region quota 分区）、ADR-013 多 Region 全双活。

成功标准：每轮的"验证"列从 HTTP 真实入口端到端通过（**禁止 smoke 脚本手动注入绕过生产者**，见审计 §6 方法论教训）。

## 2. 已锁定决策

| # | 决策 | 选择 | 理由 |
|---|---|---|---|
| D1 | 修复范围 | P0 + P1 + §9 架构 | 用户选最大范围，根治 |
| D2 | Go quota | 补齐到真能替代 Python | 用户选；含多 Region quota 分区前置 |
| D3 | 多 Region | 兑现 ADR-013 全双活 | 用户选；写亲和核心当前是空的 |
| D4 | Wave 0 前置 | **是** | retry 修复(R1a)依赖 Kafka 契约(R0b)；portal 边界(R0c)不做则 Wave 2 继续越权直写 |
| D5 | 起步轮 | R0a（安全合规硬底线） | 最小、独立、等保硬要求、风险最低 |
| D6 | PR 节奏 | 一轮一个 squash-PR，push/merge 仅在用户要求时 | 用户既有工作流 |

## 3. 范围

**In scope**：审计 §2（P0）+ §3（P1）+ §9（架构）+ Go quota 替代 + 多 Region 全双活。
**Out of scope（本轮 program）**：§4 P2 收尾（Wave 4，可后置）、sdk-gen（Phase 3 未启动）、§9-C 同步扇出 sidecar 化（仅 R3d spike 评估，不实施）。
**需产品后续定**：R2e auth 的 HMAC/OAuth2 是补实现还是设计降级（默认：补 HMAC、OAuth2 降级为"Phase 5"）。

## 4. 排序原则与依赖

- **地基先于链路**：安全合规 + Kafka 契约 + 服务边界（Wave 0）先做，防止 Wave 1 修复返工。
- **救链路用刚立的契约**：R1a retry 用 R0b 的事件契约。
- **架构纵深依赖前面**：多 Region 写亲和(R3b) 依赖 APISIX 路由归属(R1c) + Go quota region(R3a)。

依赖图（关键边）：
```
R0b(Kafka契约) ──► R1a(retry闭环)
R0c(服务边界) ──► Wave2(不再越权直写)
R1c(APISIX归属) ─┬─► R3b(多Region写亲和)
R3a(Go quota替代)─┘
R0a(安全合规) ── 独立，可最先
```

## 5. 路线图（每轮 = 一个 squash-PR）

### Wave 0 · 地基与契约
| 轮 | 范围 | 引用 | 量 | 验证 |
|---|---|---|---|---|
| **R0a** | 安全合规硬底线（详见 §6） | §2.4/2.5/2.6 | 小 | 启动断言 + 单测 |
| **R0b** | apihub_core 加 Kafka 事件契约（`task-failures`/`task-requests`/`task-status`/`api-call-events` 字段定义 + topic ownership 文档 + 生产/消费统一走契约 helper） | §9-D | 小-中 | 契约单测；现有 emit/consume 改造后不回归 |
| **R0c** | 服务边界第一步：聚合 owner 文档；portal 的 app/key 写改走 auth API（不再直写 `app`/`api_key` 表） | §9-B | 中 | portal 调 auth 端点；grep 确认无直写 |

### Wave 1 · 救活断裂链路
| 轮 | 范围 | 引用 | 量 | 验证 |
|---|---|---|---|---|
| **R1a** | retry 闭环：executor 失败投 `task-failures`（R0b 契约）+ trigger 重入 `delay_queue.schedule` + retry_task 幂等唯一约束 | §2.1/2.2/2.7 | 小 | HTTP 入口造 backend 失败 → 自动重试到死信 |
| **R1b** | dispatcher 调用事件 trace_id 改 OTel span context | §3.9 | 小 | CH 与 Jaeger 同 trace_id |
| **R1c** | 路由归属：定 APISIX 动态路由、dispatcher 退纯转发；api-registry→APISIX Admin API 下发 publish/retire | §3.1/§9-A | 中 | 发布即可调、下线即 410 |

### Wave 2 · Phase 4 上线 + stub 兑现
| 轮 | 范围 | 引用 | 量 |
|---|---|---|---|
| **R2a** | 四服务部署：notification Dockerfile + ai-gateway/billing/portal/notification Deployment + dev/staging/prod overlay | §2.3 | 中 |
| **R2b** | notification 补 `/internal/notify/send`+`/batch` + 邮件/钉钉渠道 + Channel 抽象 + 模板 | §3.3 | 大 |
| **R2c** | ai-gateway 接入 dispatcher 成唯一 AI 流式入口（统一 token 计费/限流/多 Provider） | §3.4 | 大 |
| **R2d** | GDPR 两处：`withdraw_consent` 接 `anonymize_user` + Portal 4 端点补 `/v1` | §3.5/3.6 | 小 |
| **R2e** | auth HMAC 补实现；OAuth2 设计降级（默认） | §3.10 | 中-大 |

### Wave 3 · 架构纵深
| 轮 | 范围 | 引用 | 量 | 依赖 |
|---|---|---|---|---|
| **R3a** | Go quota 补齐替代 Python：对齐响应字段/Usage 形状/Redis key/改回 Lua 原子/接 apihub_core 鉴权+RLS/补 `/health/ready`/Makefile+k8s 切镜像 | §3.8 | 大 | — |
| **R3b** | 多 Region 全双活：APISIX consumer 注入 home_region + 修 MirrorMaker/PG 逻辑订阅（per-tenant+双向）+ CH 跨区查询（trace-svc 读 `PEER_REGION_CH_HOST`）+ 切换 runbook + 季度演练 | §3.7/ADR-013 | 大 | R1c,R3a |
| **R3c** | CH 租户隔离护栏：`ch_session` 强制 tenant 过滤（参数化视图/中间件） | §9-E | 中 | — |
| **R3d** | 同步扇出 spike：auth/quota sidecar 化可行性报告（仅评估） | §9-C | 调研 | — |

### Wave 4 · P2 收尾（可后置）
trace export/compare/MinIO 堆栈；docs 参数表/Java/HTML；admin RBAC/CSV；billing 阶梯+CronJob；api-registry PUT/versions/rollback/list 过滤。（§4）

---

## 6. R0a 详细实现 spec（起步轮）

### 6.1 目标
补齐安全合规硬底线，使 RLS 旁路可审计、RLS 会话变量无注入面、密钥在 prod 不可用默认值、定时任务能真正跑起来。独立、无下游依赖、风险最低。

### 6.2 改动项（含 file:line 证据）

**(1) `admin_db_session` 写审计** — `services/libs/apihub-core/src/apihub_core/db.py:116-139`
- 现状：docstring 声称写 audit_events，实际只 `SET LOCAL app.is_platform_admin='true'` 后 yield，零审计。
- 改动：yield 前后写一条 audit_log（actor/source 由调用方通过参数或 contextvar 传入；至少记 "admin_db_session used by {service}" + 操作摘要）。提供可选 `reason` 参数；不强加给纯读的 admin 查询则用 contextvar 默认值。
- 约束：不能破坏现有调用方签名（加可选参数 + contextvar 兜底）。

**(2) `db_session` RLS 变量改参数化** — `db.py:105-106`
- 现状：`f"SET LOCAL app.tenant_id = '{ctx.tenant_id}'"`（f-string 拼接，注入面）。
- 改动：改用 `SELECT set_config('app.tenant_id', $1, true)` 参数化执行；`app.is_platform_admin` 同理。`admin_db_session`/`meta_db_session` 内的 SET 一并改。

**(3) config 密钥 prod 启动断言** — `services/libs/apihub-core/src/apihub_core/config.py:79,87,92`
- 现状：`jwt_secret="dev-only-insecure-secret"`、`pii_encryption_key="deadbeef..."`、`oss_secret_key="apihub_dev_pwd"` 均为不安全默认值，无 prod 强制。
- 改动：加 `Settings.validate_security()`（或在 `get_settings` 首次调用 / app startup）断言：当 `env == "prod"`（或显式 `REQUIRE_SECURE_SECRETS=1`）时，三个密钥不得等于默认值，否则启动失败并明确报错。dev/test 仍允许默认值。

**(4) CronJob 补 secret 卷挂载** — `deploy/k8s/base/shared/audit-archive-cronjob.yaml:30`、`data-cleanup-cronjob.yaml:30`
- 现状：`cat /etc/apihub/api-key` 但 template 无 `volumes`/`volumeMounts`。
- 改动：两个 CronJob 的 Job template 补 `volumes:[{name:api-key,secret:{secretName:apihub-admin-apikey}}]` + container `volumeMounts:[{name:api-key,mountPath:/etc/apihub,readOnly:true}]`。确认该 Secret 在各 overlay 存在（不存在则 base 用 templated Secret 占位 + overlay 注入）。

### 6.3 验证
- 单测：`admin_db_session` 调用后 audit_log 多一条；`db_session` 用含 `'` 的 tenant_id 不报错且 RLS 正确；prod env + 默认密钥启动 raise。
- `make dev-up` 后：两个 CronJob 手动触发一次（`kubectl create job --from=`），curl 带 `X-API-Key` 成功（不再 "No such file"）。
- 回归：现有 `test_db_rls.py`、`test_identity.py` 全绿。

### 6.4 风险
- (1) 写审计若同步插 PG 写，事务边界要小心（在 yield 外另起小事务，避免污染业务事务）。
- (3) 启动断言别误伤 dev/test：用 env 判断，默认值在 dev 仍合法。
- (4) Secret 名在各 overlay 一致性。

### 6.5 不做（R0a 边界）
- 不动 Kafka 契约（R0b）、不动服务边界（R0c）、不动业务服务代码（除调用方兼容）。

---

## 7. 待 review 的开放点

1. R0a (1) 的 audit 写入：同步写 PG vs 发 Kafka 异步落 audit？默认**同步小事务**（等保要求强一致可审计），若性能敏感再改异步。
2. R2e HMAC/OAuth2：默认补 HMAC、OAuth2 降级 Phase 5——需产品确认。
3. R3d sidecar spike：评估完若不可行，§9-C 标"已知接受"。

## 8. 下一步

R0a spec 经用户复核通过后 → 调 **writing-plans** 出 R0a 的逐步实施计划（TDD，每步可验证），随后进入 handoff 实现。
