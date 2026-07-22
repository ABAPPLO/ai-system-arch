# R2e — auth HMAC 签名补实现

- **日期**: 2026-07-21
- **轮次**: R2e（fix-program Wave 2 收尾）
- **base**: main = `d747fa1`（R3d #63 合后）
- **范围**: inbound HMAC 验签 + secret 轮换端点 + outbound webhook 签名 + 客户端签名 helper/doc
- **出处**: `docs/phase4-audit-findings.md` §3.10（HMAC/OAuth2 零实现）；`docs/08-observability-security.md` §7.3；`docs/superpowers/specs/2026-07-15-apihub-fix-program-design.md` line 38/78/135（默认补 HMAC、OAuth2 降级 Phase 5）
- **关联**: [[r1d-apisix-auth]] / [[r2b-notification-channels]] / [[r3d-sidecar-feasibility]]（in-process Lua 对比 sidecar）

---

## 1. 背景与决策

### 1.1 现状

API Key（`ak_<32 url-safe>`，DB 存 SHA256 hash，明文仅创建时返回一次）两条验签路径：

1. **edge 验签（R1d）**：APISIX `key-auth` per-key consumer + dispatcher 信任入口（`X-Ingress-Auth`）读 Redis 身份缓存（warm path 无 httpx 回源）。
2. **冷路径**：dispatcher HTTP 调 auth `/v1/apikey/verify`。

JWT 用于外部开发者「人」的 token（`eyJ` 本地验签）。**HMAC 与 OAuth2 完全未实现**（audit §3.10）；fix-program 已定调：补 HMAC、OAuth2 降级 Phase 5。

### 1.2 关键约束

**HMAC secret 不能存为单向 hash**——验签需用真实 secret 字节重算 HMAC 后 `compare_digest`（与 API key 仅存 SHA256 不同）。故 secret 必须**可逆存**（envelope 加密 at-rest），这是本轮数据模型的根因。

### 1.3 已定决策（brainstorm 产出）

| 决策点 | 选择 | 理由 |
|---|---|---|
| 凭据模型 | 独立 signing secret（AWS 式），envelope 加密存 | 匹配 §7.3 `sign(secret)` 语义；key 泄露但 secret 未泄不致命；secret 可独立轮换；贴合「金融/高安全」定位 |
| 验签位置 | in-app（`apihub_core`） | 完全掌控 §7.3 canonical 算法；secret 只在 PG 加密存（不进 APISIX etcd 明文）；HMAC 高安全低量，in-app 验签合理（同 AWS SigV4 服务端验）；不被 APISIX hmac-auth 插件算法锁死 |
| 范围 | inbound 验签 + secret 轮换 + outbound webhook 签名 + 客户端 helper/doc | 全部纳入本轮 |

---

## 2. 架构

HMAC 是叠加在 `api_key` 之上的**可选签名层**（per-key opt-in：`hmac_secret_encrypted` 列非空 = 该 key 走签名模式）。

### 2.1 Inbound（客户端签，平台验）

```
client → APISIX（只路由，HMAC 请求无 bearer key 不走 key-auth）
  → dispatcher → apihub_core.auth.authenticate_request
  → 检测 X-App-Key + X-Signature 头（且非 JWT 流）→ _verify_hmac
  → 取 secret：
      warm = Redis hmac_secret:{key_hash}（加密 blob）→ in-process AESGCM decrypt
      cold = auth HTTP /v1/internal/hmac-secret（admin-scoped，按 key_id）
  → 重算 canonical HMAC → compare_digest（常时）
  → timestamp ±5min + nonce Redis SETNX 600s（key 作用域）
  → set TenantContext（复用 R1d identity 缓存取 tenant/app 元数据）
```

### 2.2 Outbound（平台签 webhook body，客户端验）

```
notification consumer._deliver → sign(raw_body, secret) → X-Webhook-Signature 头 → POST
client → compare_digest
```

### 2.3 核心设计取舍

- **secret 暖路径**：Redis 存**加密 blob**（非明文），dispatcher in-process AESGCM decrypt（μs 级，远低于一次 Redis RTT）。明文只存在于 PG（加密 at-rest）+ 请求处理瞬态内存。auth 在 create/rotate 时写此缓存（同 R1d identity 缓存写入时机）。
- **冷路径**：dispatcher 不碰 PG（守 R1d 不变量），调 auth 新端点 `/v1/internal/hmac-secret`（admin-scoped，集群内 NetworkPolicy）。HMAC 高安全低量，冷首请求可接受。
- **env key 独立**：`HMAC_SECRET_KEY`（32-byte hex）专用，不复用 `AI_GATEWAY_ENCRYPTION_KEY`——爆炸半径隔离 + 各自轮换不互扰。

### 2.4 canonical 串（§7.3 对齐 + 显式消歧义）

```
canonical = f"{method}\n{raw_path_with_query}\n{timestamp}\n{sha256(body).hexdigest()}"
signature = HMAC-SHA256(secret, canonical).hexdigest()
```

> §7.3 字面只写 `path`，但 query 参与路由必签——本轮用 `raw_path_with_query`（`request.url.path` + `?` + `request.url.query`），doc 显式声明（AWS SigV4 同样签 query）。
>
> **query 保持 client 发送原样**：`request.url.query` 是 percent-encoded 原始串，服务端**不 normalize / 不 re-encode / 不 decode-then-reencode**（参数顺序、大小写、percent-casing 都按 wire 原样逐字节纳入 canonical）。客户端必须用同一规则签。spec §5 T1 覆盖此不变式。

---

## 3. 组件

### 3.1 新增

**C1. `apihub_core/crypto.py`（共享加密原语）**

镜像 `ai_gateway/crypto.py`：AESGCM，env `HMAC_SECRET_KEY`(32-byte hex)，`encrypt_secret(str)->b64` / `decrypt_secret(b64)->str`。放 apihub_core 而非 ai-gateway：auth(写)、middleware(读)、notification(outbound) 三方共用。启动时缺 key → fail-closed RuntimeError（同 ai-gateway）。

**C2. `apihub_core/signing.py`（HMAC canonical 纯函数，in/out 双向复用）**

- `canonical_string(method, raw_path_with_query, body, timestamp) -> str`
- `sign(secret, method, raw_path_with_query, body, timestamp) -> str`（HMAC-SHA256 hexdigest）
- `verify(secret, ..., provided_signature) -> bool`（内部 `hmac.compare_digest`，常时）

纯函数零依赖，既是服务端验签核心也是客户端 helper 的单一真相源（§7 doc 直接引用此实现）。

**C3. `apihub_core/auth.py: _verify_hmac(...)`（inbound 验签，挂在 authenticate_request）**

触发条件：请求带 `X-App-Key` + `X-Signature`（且非 JWT 流）。

步骤：
1. hash(X-App-Key) → identity 缓存取 tenant/app ctx（复用 R1d）→ 判定该 key 是否 enrolled（`hmac_secret_encrypted` 非空）；未 enrolled 却带签名头 → 401。
2. 取 secret：warm=Redis `hmac_secret:{key_hash}`（加密 blob）→ decrypt；miss=auth HTTP `/v1/internal/hmac-secret`。
3. timestamp ±5min。
4. nonce `SETNX t:{tenant}:hmac:nonce:{key_id}:{nonce}` TTL 600s。
5. `verify()`。

identity 缓存 entry 增一字段 `hmac_enrolled: bool`（仅布尔，不存 secret）——secret 单独存单独 TTL，便于 rotate 只失效 secret 缓存不清 identity。

**C4. auth 新端点 + repository**

- `POST /v1/internal/hmac-secret`（admin-scoped，`skip_auth_paths`，集群内 NetworkPolicy）：入 `{key_id}` → admin_db_session 取 secret → 返明文（仅冷路径 dispatcher 用，集群内 + admin bypass RLS）。等价 R1d `/v1/apikey/verify` 的冷回源。
- `auth/repository.py`：
  - `create_api_key` 接 `signing: bool` → 生成 `hmac_secret`（32-byte url-safe）→ `encrypt_secret` 存。
  - `get_hmac_secret_plaintext(key_id)`（admin_db_session）。
  - `rotate_hmac_secret(key_id)` → 新 secret + 更新加密列 + RETURNING 明文。
- `POST /v1/apps/{app_id}/api-keys` 增 body 字段 `signing: bool=false`；响应 `ApiKeyResponse` 增 `hmac_secret: str | None`（明文，仅创建返回一次，同 `api_key` 语义）。
- `POST /v1/api-keys/{key_id}/hmac-secret/rotate`：返回新明文一次 + 失效 secret Redis 缓存 + 写 audit_log。APISIX consumer 无需动（HMAC 不经 edge）。

**C5. outbound signer（notification 局部 + 通用化）**

- `notification/consumer.py:_deliver` 已签 body——标准化：头改 `X-Webhook-Signature: hmac-sha256=<hex>`。
- `webhook_subscription.secret` 列 → `secret_encrypted`（迁移见 §4）；create-webhook 改**平台生成 secret**（返明文一次），保留 client-supplied 为兼容选项（旧客户端不受影响）。
- 消费时 `decrypt_secret(hook["secret_encrypted"])` → 复用 C2 `sign()`。

### 3.2 不改

- APISIX 配置零动（HMAC 请求 pass-through，不 wired consumer 插件）。
- `verify_api_key_record` / `/v1/apikey/verify` bearer 路径不动。
- JWT 流不动。
- Go quota 不动（HMAC 不在热限流路径）。

### 3.3 单元边界自检

| 单元 | 做什么 | 怎么用 | 依赖 |
|---|---|---|---|
| C1 crypto | 加解密 secret | auth 写、C3/C5 读 | env key |
| C2 signing | canonical+签+验 | C3 验、C5 签、doc | 无 |
| C3 _verify_hmac | inbound 验签编排 | authenticate_request 调 | C1+C2+identity cache+auth HTTP |
| C4 auth routes/repo | secret 生命周期 | dispatcher 冷路径 + 管理面 | C1+RLS |
| C5 outbound signer | webhook 签名 | notification consumer | C1+C2 |

---

## 4. Schema / Migration

一个迁移 `scripts/init-db/14-hmac-secret.sql`（幂等，沿用 R2b/R3a 的 `DROP POLICY IF EXISTS`+`CREATE POLICY` 回放风格；apply 经 `make db-apply`，as owner `apihub`）。

### 4.1 `api_key` 表

```sql
ALTER TABLE api_key ADD COLUMN IF NOT EXISTS hmac_secret_encrypted text;
COMMENT ON COLUMN api_key.hmac_secret_encrypted IS
  'AESGCM-encrypted HMAC signing secret (b64). NULL = key not enrolled for HMAC signing.';
```

- **不加 NOT NULL**：opt-in，存量 key 不受影响（向后兼容，零迁移数据回填）。
- **不加索引**：secret 查询走 `key_hash` 唯一索引（`idx_api_key_hash`）或 PK `api_key.id`，无新索引。
- **RLS 继承**：`api_key` 已 `FORCE ROW LEVEL SECURITY`（01-schema:242），新列自动受既有 policy 保护。`get_hmac_secret_plaintext` 走 `admin_db_session`（bypass RLS），同 `verify_api_key_record`。

### 4.2 `webhook_subscription` 表（outbound）

```sql
ALTER TABLE webhook_subscription ADD COLUMN IF NOT EXISTS secret_encrypted text;
```

> 存量明文 `secret` 的加密回填不能在纯 SQL 完成（AESGCM 在应用层）。回填走 `scripts/init-db/14-backfill-webhook-secret.py`（§4.4）。
>
> **明文擦除用 `UPDATE ... SET secret=NULL` 原地 scrub，不用 `DROP COLUMN`**：避免「ADD+DROP 同一脚本，apply-db 顺序执行会先 DROP 明文再回填 → 存量 secret 丢失」的 ordering hazard；且 scrub 可回滚（重新 decrypt 回填即恢复明文列），DROP 不可逆。回填 .py（§4.4）在 ADD `secret_encrypted` 之后跑，回填 + scrub 明文一步完成。`secret` 列保留为 NULL 占位，后续清理轮再 DROP。

RLS 继承（06-notification 已 ENABLE/FORCE），无新 policy。

### 4.3 Redis 缓存键（新增，不进 PG）

| 键 | 内容 | TTL | 写入 | 失效 |
|---|---|---|---|---|
| `ak:{key_hash}`（identity，现有） | +`hmac_enrolled: bool` 字段 | 5min（同 R1d） | auth create/verify | revoke/rotate |
| `hmac_secret:{key_hash}`（**新增**） | 加密 secret blob（b64） | 5min | auth create/rotate | rotate/revoke/写 invalidate |

- **secret 与 identity 分键**：rotate 只 `DEL hmac_secret:{key_hash}`，不清 identity（tenant/app 元数据不变，省一次回源）。identity entry 的 `hmac_enrolled` 不随 rotate 变（仍 enrolled），仅 secret 值换。
- **明文不进 Redis**：blob 是 AESGCM 密文，decrypt 在 dispatcher in-process。Redis dump 不泄露明文。
- **负缓存**：未 enrolled key 带签名头 → 走 identity cache 的 `hmac_enrolled=False` 直接拒，不写 secret 缓存（防探测）。

### 4.4 回填流程（outbound 存量 webhook secret）

`scripts/init-db/14-backfill-webhook-secret.py`（apply-db 不直接跑此 .py，由 ops 在 apply 14-*.sql 后手动跑，或嵌入 bootstrap §1g）：

1. `admin_db_session`（bypass RLS）`SELECT id, secret FROM webhook_subscription WHERE secret IS NOT NULL AND secret_encrypted IS NULL`。
2. 对每行 `encrypt_secret(plaintext)` → `UPDATE ... SET secret_encrypted=$1, secret=NULL WHERE id=$2`（加密入新列 + 原地 scrub 明文列，一步完成）。
3. 幂等：二次跑无 `secret IS NOT NULL` 行即 no-op。
4. 无明文 secret 的行（新 env / client 之前未设 secret）→ no-op 不报错。

> 不 DROP `secret` 列：保留为 NULL 占位（见 §4.2 rationale），后续清理轮再 DROP。

### 4.5 env 新增

```
HMAC_SECRET_KEY=<32-byte hex>           # AESGCM key，与 AI_GATEWAY_ENCRYPTION_KEY 独立
HMAC_TIMESTAMP_WINDOW_SECONDS=300        # ±5min，默认 300
HMAC_NONCE_TTL_SECONDS=600               # 10min，默认 600
```

- `.env.dev` / kind configmap 注入；`deploy/k8s/services/*/deployment.yaml` 增 envFrom 或显式 env（同 `AI_GATEWAY_ENCRYPTION_KEY` 模式）。
- `apihub_core/config.Settings` 加三字段：`HMAC_SECRET_KEY` 无默认 → 缺则 `Settings()` raise（与 `pg_host` 等同级）；`HMAC_TIMESTAMP_WINDOW_SECONDS`/`HMAC_NONCE_TTL_SECONDS` 给默认（300/600）避免测试全配。

### 4.6 兼容 / 回滚

- **回滚安全**：`api_key.hmac_secret_encrypted` 可随时 `DROP COLUMN`（opt-in，存量 key 无影响）；webhook 回滚需先从 `secret_encrypted` decrypt 回 `secret` 明文列（对称，回填 .py 反向脚本）。spec 写明回滚顺序。
- **bearer 不受影响**：未带签名头的请求走原 `X-Ingress-Auth` + identity cache 快路径，零回归。
- **kind e2e**：apply-db 14 + 回填 → create HMAC key + 签名请求通 + revoke/rotate 失效（见 §5 T7）。

### 4.7 部署 gate（ops）

- env 须先配 `HMAC_SECRET_KEY`。
- `14-backfill-webhook-secret.py` 必须在 `webhook_subscription.secret` 明文被 scrub 前跑（回填 .py 本身即 ADD+scrub 一步，见 §4.4）——顺序：apply 14-*.sql（ADD `secret_encrypted`）→ 跑回填 .py（加密 + SET secret=NULL）。
- 无存量 webhook 的 env（dev/kind）无此约束，回填 .py no-op。
- ArgoCD：envFrom 注入 `HMAC_SECRET_KEY` 后才 sync（缺 key → auth/notification 启动 fail-closed）。

---

## 5. 测试

沿用 repo 约定（`asyncio_mode=auto`，conftest 注最小 env + `reset_tenant_context`，DB 测试 stub PG，端到端走 kind e2e）。

**T1 — 纯函数（`apihub_core/signing.py`，零依赖）**
- canonical 串构造（method/path/query/body/timestamp 各字段独立变异 → 串变）。
- `sign`→`verify` round-trip True；`compare_digest` 常时（错位/截断/全错均 False，不抛）。
- body 为空 → `sha256(b"")` 正确。
- query 含 `&`/`=`/编码字符 → 按 `request.url.path` + raw query 逐字节，不二次 decode。

**T2 — 加密原语（`apihub_core/crypto.py`）**
- `encrypt_secret`→`decrypt_secret` round-trip。
- 缺 env key → fail-closed RuntimeError（启动期，不延到请求）。
- 密文非确定性（AESGCM nonce 随机）→ 同明文两次密文不同 + 都能 decrypt。
- 篡改密文/nonce → decrypt 抛 `InvalidTag` → 上层转 503/401（不裸抛）。

**T3 — inbound 验签编排（`_verify_hmac`，mock secret 源）**
- enrolled key + 正确签名 → 200 + `TenantContext` 正确 set。
- 未 enrolled key 带签名头 → 401（走 identity cache `hmac_enrolled=False`，不查 secret）。
- **enrolled key 不带签名头 → 401**（key 已声明签名模式，bearer 调用应失败；防降级绕过）。
- timestamp ±300s 内通；+301s/-301s → 401 `stale timestamp`。
- nonce 首次 SETNX 成功 → 通；同 nonce 二次 → 401 `replay detected`。
- body 篡改 1 字节 → 401 `invalid signature`。
- secret warm（Redis 命中加密 blob）vs cold（auth HTTP mock）路径都覆盖。
- rotate 后旧 secret → 401（identity cache 仍 `hmac_enrolled=True`，secret 缓存已 DEL → cold 回源取新 secret → 旧签名验不过）。

**T4 — auth 端点 + repository（DB-touching，需 dev stack 或 stub PG）**
- `create_api_key(signing=True)` → 返回 `hmac_secret` 明文 + DB 列非空 + Redis secret 缓存写入。
- `create_api_key(signing=False)` → `hmac_secret` None + 列 NULL。
- `rotate_hmac_secret` → 新明文（≠旧）+ Redis `hmac_secret:{key_hash}` DEL + identity cache 不动 + audit_log 写入。
- `get_hmac_secret_plaintext` 跨租户 → admin_db_session bypass RLS 取到；非 admin caller → RLS 拒（无行）。
- `/v1/internal/hmac-secret` 非集群内 caller → 403（NetworkPolicy 级，单测 stub）。

**T5 — outbound signer（notification）**
- `_deliver` 设 `X-Webhook-Signature: hmac-sha256=<hex>` + 客户端 mock `verify` 通过。
- body 改 → 签变 → 客户端拒。
- `secret_encrypted` 为 NULL → 不签名（向后兼容无 secret subscription）。
- create-webhook 平台生成 secret → 返明文一次 + DB 存加密列；client-supplied 兼容路径 → 平台 encrypt 存。

**T6 — 回填脚本（`14-backfill-webhook-secret.py`）**
- 存量 3 行明文 secret → 跑后 3 行 `secret_encrypted` 非空 + 明文列可 DROP。
- 二次跑 no-op（幂等）。
- 无存量的 env → no-op 不报错。

**T7 — kind e2e（DATA-PLANE）**
- apply-db 14 + 回填 → 创建 HMAC key（明文返）→ curl 签名请求经 APISIX → dispatcher 验签 200 → 篡改 body 401 → replay 401 → rotate → 旧签名 401、新签名 200 → bearer key 不受影响仍 200。
- outbound：创建 webhook subscription → 触发 event → 抓 notification consumer POST 带 `X-Webhook-Signature` → mock server 验签通过。
- RLS：跨租户取 secret 不可达。

**T8 — 回归（不回归）**
- apihub-core 全量（R3c 基线 121/0/15-skip）+ auth 81/0 + notification（R2b 基线）+ trace。
- bearer/JWT 路径零回归（最关键：HMAC 是叠加，不能碰现有 200 路径）。

---

## 6. 错误处理（fail-closed，不 fail-open）

| 场景 | 响应 | 备注 |
|---|---|---|
| 带签名头但 identity cache miss + auth 不可达 | 503 `auth unreachable` | 复用 R1d 503 语义；不降级 bearer |
| enrolled key 不带签名头 | 401 `hmac signing required for this key` | 防降级绕过 |
| 未 enrolled key 带签名头 | 401 `key not enrolled for hmac` | 走 identity cache 布尔，不查 secret |
| timestamp 缺失/非数字/超窗 | 401 `invalid/stale timestamp` | 缺头也算 invalid |
| nonce 缺失/重放 | 401 `invalid/replay nonce` | SETNX 失败即重放 |
| signature 不匹配 | 401 `invalid signature` | `compare_digest` 常时，不泄露哪步错 |
| secret 缓存密文损坏（decrypt 抛 InvalidTag） | 503 + log + DEL 该缓存键 | 不当 401（非客户端错，防 client 误判）；下次冷回源 |
| secret env key 缺失（启动期） | RuntimeError fail-closed | 不启动，不进请求路径 |
| outbound `secret_encrypted` NULL | 不签名（不发 X-Webhook-Signature） | 向后兼容，log warn |
| 回填 .py 在 DROP COLUMN 前 | spec 强制顺序 gate（§4.7） | ops 误序 → 存量 secret 丢失（spec 警示） |

**错误码**：复用 `ErrorCode.UNAUTHORIZED`（401）+ `INTERNAL`（503），不新增枚举（避免 errors.py 膨胀；消息串区分场景）。

**常时性**：所有签名比对走 `hmac.compare_digest`；timestamp/nonce 判定不区分「哪步失败」（统一 401 invalid signature，防时序探测）。

**审计**：rotate 写 `audit_log`（`audit_reason="hmac_secret_rotation"`，复用 R2e GDPR 的 `admin_db_session` 审计路径，不带 PII）；HMAC 验签失败不写审计（高频，仅 log.info）。

---

## 7. 客户端 helper/doc

- `apihub_core/signing.py` 的 `sign()` 即客户端参考实现的单一真相源。
- 新增 `docs/hmac-signing.md`：canonical 串定义 + headers 列表（`X-App-Key`/`X-Timestamp`/`X-Signature`/`X-Nonce`）+ Python 示例（直接 `from apihub_core.signing import sign`）+ curl 示例（`openssl dgst -sha256 -hmac`）。
- sdk-gen 服务（Phase 3 未启动）后续可基于此 doc 生成多语言 SDK。

---

## 8. 不在本轮范围（defer）

- **OAuth2**：降级 Phase 5（fix-program 已定）。
- **APISIX edge hmac-auth 插件**：本轮 in-app 验签，不 wired APISIX（secret 不进 etcd）。
- **secret 自动轮换策略**（如 90 天强制）：本轮仅提供 rotate 端点，策略由 admin/ops 手动或后续 CronJob。
- **HMAC 与 scope 结合的细粒度授权**：本轮 HMAC 仅做「证明持有 secret」，scope 仍走现有 `required_scopes`（auth.py:22 死参，另轮修）。
- **sdk-gen 多语言 SDK**：Phase 3。
