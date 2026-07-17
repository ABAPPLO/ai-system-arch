# R2b spec — notification 渠道层（邮件/钉钉 + Channel 抽象 + 模板 + 主动发送）

日期：2026-07-17 · 分支 `fix/r2b-notification-channels`（已建）· 依据：fix-program 设计 §5 Wave 2 R2b（引用 §3.3）。

## 问题

R2a 已把 notification 服务部署进 kind 并跑通 **webhook 被动推送链路**（Kafka `api-call-events` → 匹配 `webhook_subscription` → HMAC 推送 + 重试）。但 roadmap（§5 R2b）要求的「主动通知发渠道」层**完全缺失**：

- 无 `/internal/notify/send` + `/batch` —— 其它服务（executor 任务完成、quota 用量告警、billing 开票）无法主动发通知。
- 无**渠道抽象**——只有 webhook 一条硬编码路径，邮件/钉钉无法插。
- 无**邮件 / 钉钉渠道**实现。
- 无**模板系统**——通知文案无处统一管理、无变量校验。
- 无**投递日志**——发了什么、成败与否、GDPR 留痕全无。

另：R2a follow-up 记的 `webhook_subscription UndefinedTableError`，经查不是脚本缺（`scripts/init-db/06-notification.sql` 含完整 DDL，幂等），而是 **kind/dev 的 `pg-data` 卷是旧的**——init-db 只在空卷首启跑一次（`docker-compose.dev.yml:48` 挂了 `./scripts/init-db:/docker-entrypoint-initdb.d:ro`），之后新加的 `06/09` 等脚本从不应用。脚本是 `CREATE ... IF NOT EXISTS` 幂等的，缺的是一个**对运行中 PG 幂等回放 init-db 的步骤**。

## 范围（已与用户确认）

- **完整 R2b**：Channel 抽象 + 邮件（aiosmtplib）+ 钉钉（自定义机器人 HMAC 签名）+ `/internal/notify/send` + `/batch` + 模板系统（平台全局 seed 表 + JSON Schema 校验 + locale 回退）+ 投递日志表。
- **渠道配置**：per-tenant `notification_channel_config` 表（与 `webhook_subscription` 的 per-tenant 模式一致，RLS 自然适用）；邮件 tenant 无配置时回退平台 `NOTIFICATION_SMTP_*` env。
- **模板**：平台全局 seed 表（无 tenant_id），按 `(code, channel_type, locale)` 主键，init-db seed；渲染走轻量 `{{var}}` 替换 + `jsonschema` 校验，**不上 Jinja2**（依赖重、沙箱面）。
- **kind schema-apply**：新 `scripts/k8s/apply-db.sh` + `make db-apply` + bootstrap 回放，幂等补齐 `webhook_subscription`/`consent`/3 张新表，解锁所有表依赖的 e2e。

## 不做（R2b 边界）

- **飞书/Lark 渠道**（roadmap 只指名邮件/钉钉；Channel 抽象预留扩展点，加新渠道不改 send 主路径）。
- **webhook 并入 Channel 抽象**——webhook 维持现有 Kafka 驱动链路（事件被动推送），与主动 `notify/send` 是两条路径；强行合并增加复杂度且 YAGNI。
- **per-tenant 模板覆盖**（平台全局 seed 已够；租户自定义文案是 Wave 4 收尾）。
- **notify/send 自动重试 / 接 retry-svc**——本轮 send 失败写 `notification_log` 即止；retry-svc 当前消费 Kafka 失败事件，notify/send 是同步 HTTP，集成形态另议。
- **prod 迁移 Job**——apply-db 本轮只覆盖 dev/kind（host compose PG）；prod 走独立迁移 Job，留 follow-up。

## 设计

### 数据模型（新 `scripts/init-db/11-notification-channels.sql`，幂等）

所有表沿用项目约定：`text` PK + `tenant_id` + `created_at/updated_at` + RLS policy + `set_updated_at` trigger + `GRANT ... TO apihub_app`。镜像 `06-notification.sql` 结构。

#### `notification_channel_config`（per-tenant，RLS，走 `db_session`）
```sql
CREATE TABLE IF NOT EXISTS notification_channel_config (
    id           text PRIMARY KEY,
    tenant_id    text NOT NULL,
    channel_type text NOT NULL,                -- email / dingtalk
    name         text NOT NULL DEFAULT 'default',
    config       jsonb NOT NULL DEFAULT '{}'::jsonb,
    status       text NOT NULL DEFAULT 'active', -- active / disabled
    created_at   timestamptz NOT NULL DEFAULT NOW(),
    updated_at   timestamptz NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, channel_type, name)
);
```
- `config` 形状按 channel_type 分：
  - `email`：`{smtp_host, smtp_port, smtp_user, smtp_password, from_addr, use_tls}`
  - `dingtalk`：`{webhook_url, secret}`
- RLS：`tenant_isolation_select`（`tenant_id = rls_tenant_filter() OR admin`）+ `tenant_isolation_modify`。

> **设计决策**：webhook 的 modify policy 是 `rls_is_platform_admin()` only（admin 写）。channel_config 是租户自服务（注册自己的钉钉群/SMTP），modify policy 放宽为 `tenant_id = rls_tenant_filter()`（租户能写自己的行）。两者不同，spec 显式声明。

#### `notification_template`（平台全局，无 tenant_id，走 `admin_db_session`）
```sql
CREATE TABLE IF NOT EXISTS notification_template (
    code             text NOT NULL,
    channel_type     text NOT NULL,            -- email / dingtalk
    locale           text NOT NULL DEFAULT 'zh-CN',
    subject_tpl      text NOT NULL DEFAULT '',
    body_tpl         text NOT NULL,
    variables_schema jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at       timestamptz NOT NULL DEFAULT NOW(),
    PRIMARY KEY (code, channel_type, locale)
);
```
- 无 tenant_id、无 RLS（平台参考数据）。读走 `admin_db_session()`（与 consumer 读 webhook 一致——参考数据/跨租户聚合）。
- seed（`ON CONFLICT DO NOTHING`，幂等）：`task_complete`[email+dingtalk]、`quota_warning`[email]、`invoice_ready`[email]，locale `zh-CN`。
- `variables_schema` 是 JSON Schema（draft-07），渲染前校验 caller 传的 variables。

#### `notification_log`（per-tenant，RLS，走 `db_session`）
```sql
CREATE TABLE IF NOT EXISTS notification_log (
    id             text PRIMARY KEY,
    tenant_id      text NOT NULL,
    template_code  text NOT NULL,
    channel_type   text NOT NULL,
    recipient      text NOT NULL,
    status         text NOT NULL,              -- sent / failed
    error          text DEFAULT '',
    provider_msg_id text DEFAULT '',
    created_at     timestamptz NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_notiflog_tenant_time ON notification_log(tenant_id, created_at DESC);
```
- 每次 send 尝试写一条（sent/failed）。GDPR 留痕（R2d `anonymize_user` 会清）。
- RLS：`tenant_isolation_select`（租户看自己的投递历史）+ `tenant_isolation_modify`（admin only，日志只由服务写）。

### Channel 抽象（`services/services/notification/src/notification/channels/`）

```
channels/
  __init__.py     # 暴露 Channel, NotificationMessage, SendResult, registry
  base.py         # Channel ABC + NotificationMessage + SendResult dataclass
  email.py        # EmailChannel
  dingtalk.py     # DingTalkChannel
  registry.py     # ChannelRegistry（type -> Channel 实例，import 时注册）
```

- **`NotificationMessage`**（dataclass）：`recipient: str`、`subject: str`、`body: str`、`channel_type: str`、`config: dict`、`meta: dict`（模板 code 等透传）。
- **`SendResult`**（dataclass）：`success: bool`、`error: str | None`、`provider_msg_id: str | None`。
- **`Channel`**（ABC）：`channel_type: ClassVar[str]`；`async def send(self, message: NotificationMessage) -> SendResult`。
- **`EmailChannel`**：`aiosmtplib.SMTP(host, port, use_tls=config.use_tls, start_tls=not use_tls)`；`login(user, password)`；`sendmail(from_addr, [recipient], _mime_message(subject, body, from_addr, recipient))`。`config` 缺关键字段（`smtp_host`）→ 回退**平台 env** `os.environ.get("NOTIFICATION_SMTP_HOST/PORT/USER/PASSWORD/FROM_ADDR/USE_TLS")`（**不**进 `apihub_core.config.Settings`，避免改共享库；env 未设且 tenant 无配置 → `SendResult(success=False, error="no smtp config")`）。网络异常 → `SendResult(success=False, error=str(e))`（不抛，由 caller 记 log）。
- **`DingTalkChannel`**：自定义机器人加签——`ts = 当前毫秒`；`string_to_sign = f"{ts}\n{secret}"`；`sign = urlquote(base64(hmac_sha256(secret, string_to_sign)))`；POST `f"{webhook_url}&timestamp={ts}&sign={sign}"`，body `{"msgtype":"markdown","markdown":{"title":subject,"text":body}}`；成功判定 `resp.json()["errcode"] == 0`，取 `msgid` 作 `provider_msg_id`。`secret` 为空则不签名（直接 POST webhook_url，兼容老式机器人）。
- **`ChannelRegistry`**：单例 dict `{"email": EmailChannel(), "dingtalk": DingTalkChannel()}`；`get(channel_type)` 找不到 → `ApiError(ErrorCode.INVALID_INPUT, "unsupported channel_type")`。

### 渲染（`notification/renderer.py`）

```
async def render(conn, *, code, channel_type, variables, locale) -> tuple[str, str]:
    # 1) locale 回退：查 (code, channel_type, locale) → 查 (..., 'zh-CN') → 查任一 → 否则 NOT_FOUND
    # 2) jsonschema.validate(variables, row.variables_schema) 失败 → INVALID_INPUT
    # 3) subject/body 做 {{var}} 替换（re.sub(r"\{\{(\w+)\}\}", ...)，缺 key 留空）
    # 4) 返回 (subject, body)
```
- `jsonschema` 加进 notification deps。
- 模板读取在 caller 已开的 `admin_db_session` 事务内（传 conn 进来，避免重复开事务）。

### 端点（`routes.py` 增，沿用 `register_routes(app)` + `@app.post` 模式）

**`POST /v1/internal/notify/send`**（X-API-Key tenant-scoped；渠道配置按 tenant 解析）
```jsonc
// req
{ "template_code": "task_complete", "channel_type": "dingtalk",
  "recipient": "（钉钉可空，群来自 config）", "variables": {"task_id":"t_1","task_name":"..."}, "locale": "zh-CN" }
// resp 200
{ "success": true, "provider_msg_id": "mid_xx" }   // 失败: {"success": false, "error": "..."}
```
流程：`require_tenant()` → `db_session` 内：读 `notification_channel_config`(tenant+type, status=active)（缺 email 配置→用平台 env 兜底）→ `admin_db_session` 内 `render()` → `ChannelRegistry.get(type).send()` → 写 `notification_log` → 返回 `SendResult`。失败也写 log（status=failed）+ 返回 200（send 失败不是 HTTP 错，是业务结果）。

**`POST /v1/internal/notify/batch`**：`list[NotifyRequest]` → `asyncio.gather(*[_handle(r) for r in reqs], return_exceptions=True)` → 返回 `list[SendResult]`（异常项转 `SendResult(success=False, error=str(e))`）。

**`GET/POST/PUT/DELETE /v1/notification/channel-configs[/{id}]`**：per-tenant 渠道配置 CRUD（`require_tenant()`，RLS 保证只动自己的）。POST/PUT body：`{channel_type, name, config, status}`。

现有 `/v1/notification/webhooks*` CRUD + `/health` 不动。

**`models.py` 增**：`NotifyRequest`、`NotifyResult`(=SendResult 的 API 形)、`ChannelConfigCreate`、`ChannelConfigUpdate`、`ChannelConfigResponse`。

### 基建（kind schema-apply）

- **新 `scripts/k8s/apply-db.sh`**：
  ```bash
  #!/usr/bin/env bash
  set -euo pipefail
  # 幂等回放 init-db/*.sql 到运行中的 apihub-pg（脚本本身 CREATE...IF NOT EXISTS / ON CONFLICT 幂等）
  docker exec -i apihub-pg psql -U apihub_app -d apihub \
    < <(cat scripts/init-db/0*.sql scripts/init-db/1*.sql)
  ```
  （用 glob 收集所有 init-db 脚本，按文件名序应用。）
- **`Makefile`**：加 `db-apply` target（调 `bash scripts/k8s/apply-db.sh`）。
- **`scripts/kind/bootstrap.sh`**：PG `wait_ready` 之后（§1d 之后）、构建镜像之前，调 `bash scripts/k8s/apply-db.sh`，回显 `\dt` 行数。
- prod 迁移 Job：本轮不做（边界）。

### Deps

`services/services/notification/pyproject.toml` += `aiosmtplib>=3,<4`、`jsonschema>=4,<5`。Dockerfile 不改（pip 清华源已在 builder，R2a 加过）。

## 改动清单

### ① SQL（新）
- `scripts/init-db/11-notification-channels.sql`（3 表 + RLS + trigger + GRANT + seed 模板，幂等）。

### ② notification 服务代码（新 + 改）
- 新 `channels/{__init__,base,email,dingtalk,registry}.py`。
- 新 `renderer.py`。
- 改 `routes.py`：加 `/v1/internal/notify/send`、`/v1/internal/notify/batch`、`/v1/notification/channel-configs` CRUD。
- 改 `models.py`：加 Notify/ChannelConfig 系列。
- 改 `repository.py`：channel_config CRUD + notification_log 写 + template 读。
- 改 `pyproject.toml`：+ aiosmtplib、jsonschema。

### ③ 基建
- 新 `scripts/k8s/apply-db.sh`。
- 改 `Makefile`：+ db-apply target。
- 改 `scripts/kind/bootstrap.sh`：PG ready 后调 apply-db。

### ④ 测试（新 + 改，沿用 `tests/conftest.py` mock 模式）
- 新 `tests/test_renderer.py`：locale 回退、`{{var}}` 替换、jsonschema 校验失败。
- 新 `tests/test_channels.py`：EmailChannel（mock aiosmtplib.SMTP）、DingTalkChannel（monkeypatch httpx，验签名 URL + errcode 解析）、平台 SMTP 回退。
- 改 `tests/test_routes.py`：send 成功/失败写 log、batch、channel-config CRUD（mock channel.send 不真发）。
- DB-touching 路径按现有 conftest 用 stub（多数单测 stub PG；e2e 在 kind 真跑）。

## 验证（走真实入口）

- **单测**：`pytest services/services/notification/tests -v` 全绿；`ruff` + `mypy` 过。
- **kustomize build**：`deploy/k8s/overlays/kind` 仍有效（本轮不改 manifest，notification deployment 已在 R2a）。
- **kind e2e**（`bootstrap.sh` 跑 apply-db 后）：
  1. `docker exec apihub-pg psql -U apihub_app -d apihub -c "\dt"` 含 `webhook_subscription`、`consent`（若 09 在）、`notification_channel_config`、`notification_template`、`notification_log`。
  2. seed 模板在：`SELECT code,channel_type,locale FROM notification_template;` ≥ 4 行。
  3. `POST /v1/notification/channel-configs`（dingtalk，config={webhook_url,secret}）→ 200。
  4. `POST /v1/internal/notify/send`（template_code=task_complete, channel_type=dingtalk, variables={...}）→ `{success:...}`，`notification_log` 落一条（sent/failed）。邮件同理（mock 或真实 SMTP）。
  5. batch 并发 → list 结果、log 行数 == 请求数。
  6. webhook consumer 不回归（仍连 Kafka，无 traceback）。

## 风险

- **aiosmtplib 异步 SMTP 在 readOnlyRootFilesystem 下**：SMTP 客户端不写盘，应无影响；TLS cert 读 `/etc/ssl/certs`（只读可读）不挡。需 e2e 验。
- **钉钉加签算法错**：时间戳须毫秒、`string_to_sign` 须含换行、base64 后 urlquote。写错 → 钉钉返 `errcode != 0`。单测用固定 ts+secret 断言生成的 sign 串（对照钉钉官方示例值）。
- **email 平台回退 vs per-tenant 配置优先级**：tenant 有 active email config → 用之；否则平台 env；都没有 → `success=False, error="no smtp config"`，写 failed log。spec 已定。
- **`/v1/internal/notify/send` 的鉴权**：tenant-scoped（X-API-Key）——内部调用方（executor/billing）须带代表该 tenant 的 key。服务级（跨租户）发送本轮不做。
- **apply-db 幂等性**：init-db 脚本必须全幂等（CREATE...IF NOT EXISTS / ON CONFLICT / DROP+CREATE policy）。新增的 `11-*.sql` 严格遵守；回放既有脚本前先核验无裸 `CREATE TABLE`（不带 IF NOT EXISTS）——若有，apply-db 会对已存在表报错。**plan 阶段逐脚本核验幂等**。
- **consent 表（09）**：apply-db 会顺带建 `consent`（若 09 脚本幂等）——bonus（R2d 要用），不在 R2b 验证范围；若 09 不幂等致 apply-db 失败，需修 09（plan 阶段核）。

## 依赖

- 无前置轮次硬依赖（webhook 链路 R2a 已通）。apply-db 修的 init-db 幂等性是本轮融资自带验收项。

## 与后续轮次关系

- **R2d（GDPR）**：`notification_log` 是 `anonymize_user` 要清的表之一（已为此加 tenant_id + log 设计）。
- **Wave 4**：per-tenant 模板覆盖、飞书渠道、notify/send 接 retry-svc —— 均为本轮显式 deferral。
