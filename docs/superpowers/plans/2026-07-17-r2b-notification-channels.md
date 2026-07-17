# R2b notification 渠道层 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 notification 服务现有 webhook 被动推送之上，加「主动通知发渠道」层（邮件/钉钉 + Channel 抽象 + 模板 + `/internal/notify/send`/`/batch` + 投递日志），并修 kind schema-apply 让所有 init-db 表真正落地。

**Architecture:** 新增 3 张 PG 表（`notification_channel_config` per-tenant / `notification_template` 平台全局 seed / `notification_log` per-tenant）。`Channel` ABC + Email/DingTalk 实现 + Registry。`renderer` 做 `{{var}}` 替换 + jsonschema 校验 + locale 回退。`/v1/internal/notify/send`+`/batch` 编排：解析渠道配置→渲染→channel.send→写 log。`scripts/k8s/apply-db.sh` 幂等回放 init-db（须先把既有非幂等脚本改幂等）。

**Tech Stack:** Python 3.11 / FastAPI / asyncpg direct（no SQLAlchemy）/ aiosmtplib（SMTP）/ jsonschema（校验）/ httpx（钉钉 webhook）/ asyncpg RLS / pytest asyncio_mode=auto。

## Global Constraints

- **RLS 分工**：tenant-scoped 表（`notification_channel_config`、`notification_log`）走 `db.db_session()`；平台全局参考数据（`notification_template`）走 `db.admin_db_session()`（镜像 `consumer._get_active_webhooks`）。
- **Channel.send 不抛异常**：任何失败返回 `SendResult(success=False, error=str(e))`，由 caller 写 `notification_log`。
- **init-db 全幂等**：所有脚本须可安全回放——`CREATE TABLE/INDEX ... IF NOT EXISTS`、`INSERT ... ON CONFLICT DO NOTHING`、每个 `CREATE POLICY` 前置 `DROP POLICY IF EXISTS`。
- **路由调 repository 用模块属性**（`repository.xxx(...)`），便于测试 `monkeypatch.setattr(repo_mod, "xxx", fake)`——镜像现有 webhook 路由。
- **测试**：`httpx.ASGITransport(app)` + monkeypatch `apihub_core.auth.authenticate_request`（设 `TenantContext(tenant_id="t_default")`）+ monkeypatch repository 函数 / `apihub_core.db.*_db_session`（asynccontextmanager yield `_FakeConn`）。DB-touching 单测 stub PG；真 DB 验证在 Task 7 kind e2e。
- **新依赖**：notification `pyproject.toml` += `aiosmtplib>=3,<4`、`jsonschema>=4,<5`。Dockerfile 不改（pip 清华源 R2a 已加）。
- **ErrorCode**：用 `apihub_core.errors.ApiError(ErrorCode.INVALID_INPUT/NOT_FOUND, ...)`。
- **钉钉加签**：`ts`（毫秒）；`string_to_sign = f"{ts}\n{secret}"`；`sign = urllib.parse.quote_plus(base64.b64encode(hmac.new(secret.encode(), string_to_sign.encode(), hashlib.sha256).digest()))`；URL `f"{webhook_url}&timestamp={ts}&sign={sign}"`（webhook_url 已含 `access_token`）；`secret` 空→不签名。
- 每个任务结束 commit；分支 `fix/r2b-notification-channels`。

## File Structure

| 文件 | 责任 | 任务 |
|---|---|---|
| `scripts/init-db/01-schema.sql`（改） | 加 `IF NOT EXISTS` / `DROP POLICY IF EXISTS`（幂等化） | T1 |
| `scripts/init-db/02-seed.sql`（改） | `INSERT ... ON CONFLICT DO NOTHING` | T1 |
| `scripts/init-db/05-billing.sql`（改） | `INSERT ... ON CONFLICT DO NOTHING` | T1 |
| `scripts/init-db/11-notification-channels.sql`（新） | 3 表 + RLS + trigger + GRANT + seed 模板 | T1 |
| `scripts/k8s/apply-db.sh`（新） | 幂等回放 init-db → 运行中 PG | T2 |
| `Makefile`（改） | `db-apply` target | T2 |
| `scripts/kind/bootstrap.sh`（改） | PG ready 后调 apply-db | T2 |
| `services/services/notification/pyproject.toml`（改） | +aiosmtplib / +jsonschema | T3 |
| `services/services/notification/src/notification/channels/__init__.py`（新） | 暴露 Channel/Message/SendResult/registry | T3 |
| `.../channels/base.py`（新） | Channel ABC + dataclass | T3 |
| `.../channels/email.py`（新） | EmailChannel（aiosmtplib + env 回退） | T3 |
| `.../channels/dingtalk.py`（新） | DingTalkChannel（HMAC 加签 + httpx） | T3 |
| `.../channels/registry.py`（新） | ChannelRegistry | T3 |
| `.../notification/renderer.py`（新） | render（locale 回退 + jsonschema + {{var}}） | T4 |
| `.../notification/repository.py`（改） | +channel_config CRUD / +insert_log / +render_template | T5 |
| `.../notification/models.py`（改） | +NotifyRequest/NotifyResult/ChannelConfig* | T6 |
| `.../notification/routes.py`（改） | +notify/send /batch / +channel-configs CRUD | T6 |
| `services/services/notification/tests/test_channels.py`（新） | Email/DingTalk 单测 | T3 |
| `services/services/notification/tests/test_renderer.py`（新） | render 单测 | T4 |
| `services/services/notification/tests/test_routes.py`（改） | +send/batch/channel-config 测试 | T6 |

---

## Task 1: init-db 幂等化 + `11-notification-channels.sql`

**Files:**
- Modify: `scripts/init-db/01-schema.sql`、`scripts/init-db/02-seed.sql`、`scripts/init-db/05-billing.sql`
- Create: `scripts/init-db/11-notification-channels.sql`
- Test: 对运行中 PG 两次回放无错（在 Task 2 的 apply-db 跑出，本任务先 psql 语法自检）

**Interfaces:**
- Produces: 3 张新表 + 既有脚本可安全回放。下游 T2/T5/T6 依赖表结构。

- [ ] **Step 1: 幂等化 `01-schema.sql`**

对所有裸 `CREATE INDEX <name> ...` → `CREATE INDEX IF NOT EXISTS <name> ...`（约 20 处，如 `01-schema.sql:29` `CREATE INDEX idx_tenant_parent ON ...` → `CREATE INDEX IF NOT EXISTS idx_tenant_parent ON ...`）。

对每个 `CREATE POLICY <name> ON <tbl> ...`（`01-schema.sql:260` 起多处），**前面**加一行 `DROP POLICY IF EXISTS <name> ON <tbl>;`。例：
```sql
DROP POLICY IF EXISTS tenant_isolation_select ON tenant_member;
CREATE POLICY tenant_isolation_select ON tenant_member ...
```
（`06-notification.sql` 已是此模式，照抄。）

- [ ] **Step 2: 幂等化 `02-seed.sql` 与 `05-billing.sql`**

每个 `INSERT INTO <tbl> (...) VALUES (...)` 语句末尾加 `ON CONFLICT DO NOTHING`（无指定列——任意唯一/PK 冲突即跳过）。`02-seed.sql:14/29/43/51/64/80/86/106/112/121/127` 与 `05-billing.sql:17/38`。注意：多 `VALUES (...),(...)` 批量 INSERT 加一个 `ON CONFLICT DO NOTHING` 即可。

- [ ] **Step 3: 核验其余脚本幂等**

`grep -nE "CREATE TABLE |CREATE INDEX |INSERT INTO|CREATE POLICY" scripts/init-db/{00,03,04,06,07,08,09,10,99}*.sql`，对任何非幂等语句同法处理。`06-notification.sql`/`09-consent.sql` 已幂等（CREATE...IF NOT EXISTS），跳过。

- [ ] **Step 4: 写 `11-notification-channels.sql`**

```sql
-- R2b notification 渠道层：channel_config / template / log
BEGIN;

-- ===== notification_channel_config（per-tenant，租户可读写自己的）=====
CREATE TABLE IF NOT EXISTS notification_channel_config (
    id           text PRIMARY KEY,
    tenant_id    text NOT NULL,
    channel_type text NOT NULL,
    name         text NOT NULL DEFAULT 'default',
    config       jsonb NOT NULL DEFAULT '{}'::jsonb,
    status       text NOT NULL DEFAULT 'active',
    created_at   timestamptz NOT NULL DEFAULT NOW(),
    updated_at   timestamptz NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, channel_type, name)
);
CREATE INDEX IF NOT EXISTS idx_channelcfg_tenant ON notification_channel_config(tenant_id, channel_type, status);
ALTER TABLE notification_channel_config ENABLE ROW LEVEL SECURITY;
ALTER TABLE notification_channel_config FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation_select ON notification_channel_config;
CREATE POLICY tenant_isolation_select ON notification_channel_config
    FOR SELECT USING (tenant_id = rls_tenant_filter() OR rls_is_platform_admin());
DROP POLICY IF EXISTS tenant_isolation_modify ON notification_channel_config;
CREATE POLICY tenant_isolation_modify ON notification_channel_config
    FOR ALL USING (tenant_id = rls_tenant_filter() OR rls_is_platform_admin())
    WITH CHECK (tenant_id = rls_tenant_filter() OR rls_is_platform_admin());
DROP TRIGGER IF EXISTS set_updated_at ON notification_channel_config;
CREATE TRIGGER set_updated_at BEFORE UPDATE ON notification_channel_config
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();

-- ===== notification_template（平台全局参考数据，无 tenant_id / 无 RLS）=====
CREATE TABLE IF NOT EXISTS notification_template (
    code             text NOT NULL,
    channel_type     text NOT NULL,
    locale           text NOT NULL DEFAULT 'zh-CN',
    subject_tpl      text NOT NULL DEFAULT '',
    body_tpl         text NOT NULL,
    variables_schema jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at       timestamptz NOT NULL DEFAULT NOW(),
    PRIMARY KEY (code, channel_type, locale)
);

-- ===== notification_log（per-tenant；select 租户可读自己，modify 仅 admin/服务）=====
CREATE TABLE IF NOT EXISTS notification_log (
    id              text PRIMARY KEY,
    tenant_id       text NOT NULL,
    template_code   text NOT NULL,
    channel_type    text NOT NULL,
    recipient       text NOT NULL,
    status          text NOT NULL,
    error           text NOT NULL DEFAULT '',
    provider_msg_id text NOT NULL DEFAULT '',
    created_at      timestamptz NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_notiflog_tenant_time ON notification_log(tenant_id, created_at DESC);
ALTER TABLE notification_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE notification_log FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation_select ON notification_log;
CREATE POLICY tenant_isolation_select ON notification_log
    FOR SELECT USING (tenant_id = rls_tenant_filter() OR rls_is_platform_admin());
DROP POLICY IF EXISTS tenant_isolation_modify ON notification_log;
CREATE POLICY tenant_isolation_modify ON notification_log
    FOR ALL USING (rls_is_platform_admin()) WITH CHECK (rls_is_platform_admin());

-- ===== seed 模板（幂等）=====
INSERT INTO notification_template (code, channel_type, locale, subject_tpl, body_tpl, variables_schema) VALUES
  ('task_complete', 'email', 'zh-CN',
   '【APIHub】任务完成：{{task_name}}',
   '您的任务 {{task_name}}（ID: {{task_id}}）已完成。',
   '{"type":"object","required":["task_id","task_name"],"properties":{"task_id":{"type":"string"},"task_name":{"type":"string"}}}'::jsonb),
  ('task_complete', 'dingtalk', 'zh-CN',
   '任务完成',
   '### 任务完成\n\n**{{task_name}}**（ID: `{{task_id}}`）已执行完成。',
   '{"type":"object","required":["task_id","task_name"],"properties":{"task_id":{"type":"string"},"task_name":{"type":"string"}}}'::jsonb),
  ('quota_warning', 'email', 'zh-CN',
   '【APIHub】配额预警：已用 {{used_pct}}%',
   '您本计费周期配额已使用 {{used_pct}}%（{{used}} / {{quota}}），请及时关注。',
   '{"type":"object","required":["used_pct","used","quota"],"properties":{"used_pct":{"type":"string"},"used":{"type":"string"},"quota":{"type":"string"}}}'::jsonb),
  ('invoice_ready', 'email', 'zh-CN',
   '【APIHub】账单 {{period}} 已生成',
   '您 {{period}} 的账单已生成，应付金额 {{amount}} 元。',
   '{"type":"object","required":["period","amount"],"properties":{"period":{"type":"string"},"amount":{"type":"string"}}}'::jsonb)
ON CONFLICT (code, channel_type, locale) DO NOTHING;

GRANT SELECT, INSERT, UPDATE, DELETE ON notification_channel_config, notification_log TO apihub_app;
GRANT SELECT ON notification_template TO apihub_app;

COMMIT;
```

- [ ] **Step 5: psql 语法自检**

Run:
```bash
docker exec -i apihub-pg psql -U apihub_app -d apihub --single-transaction -v ON_ERROR_STOP=1 < scripts/init-db/11-notification-channels.sql
```
Expected: `COMMIT`（若 apihub-pg 未起，改用 `psql --dry-run` 或在 Task 2 e2e 跑；至少确认无语法错）。

- [ ] **Step 6: Commit**

```bash
git add scripts/init-db/01-schema.sql scripts/init-db/02-seed.sql scripts/init-db/05-billing.sql scripts/init-db/11-notification-channels.sql
git commit -m "feat(r2b): init-db 幂等化 + notification 渠道层 3 表/seed（channel_config/template/log）"
```

---

## Task 2: kind schema-apply（`apply-db.sh` + Makefile + bootstrap）

**Files:**
- Create: `scripts/k8s/apply-db.sh`
- Modify: `Makefile`（加 `db-apply`）、`scripts/kind/bootstrap.sh`（PG ready 后调 apply-db）

**Interfaces:**
- Consumes: Task 1 的幂等 init-db 脚本。
- Produces: `make db-apply` + bootstrap 自动回放，使 kind PG 含全表。

- [ ] **Step 1: 写 `scripts/k8s/apply-db.sh`**

```bash
#!/usr/bin/env bash
# 幂等回放 scripts/init-db/*.sql 到运行中的 apihub-pg。
# 前提：init-db 脚本全幂等（CREATE...IF NOT EXISTS / ON CONFLICT DO NOTHING / DROP+CREATE POLICY）。
set -euo pipefail
cd "$(dirname "$0")/../.."

PG_USER="${PG_USER:-apihub_app}"
PG_DB="${PG_DB:-apihub}"

SQL_FILES=$(ls scripts/init-db/*.sql | sort)

echo "==> apply-db: replaying init-db scripts to apihub-pg"
cat $SQL_FILES | docker exec -i apihub-pg psql -U "$PG_USER" -d "$PG_DB" -v ON_ERROR_STOP=1

echo "==> tables now:"
docker exec -i apihub-pg psql -U "$PG_USER" -d "$PG_DB" -tAc \
  "SELECT count(*) FROM pg_tables WHERE schemaname='public';"
```
`chmod +x scripts/k8s/apply-db.sh`。

- [ ] **Step 2: Makefile 加 `db-apply`**

在 Makefile 合适位置（`dev-psql` target 附近）加：
```make
db-apply:  ## 幂等回放 init-db/*.sql 到运行中的 apihub-pg（dev/kind）
	bash scripts/k8s/apply-db.sh
```
（缩进用 Tab。）

- [ ] **Step 3: bootstrap.sh 在 PG ready 后调 apply-db**

`scripts/kind/bootstrap.sh` 的 `wait_ready "redis" ...` 之后（§1d 区块末尾，构建镜像 §4 之前）插入：
```bash
# 1f) 幂等回放 init-db（pg-data 卷可能旧，首启未含后加脚本；脚本全幂等可安全回放）
echo "=== apply-db (idempotent init-db replay) ==="
bash scripts/k8s/apply-db.sh
```

- [ ] **Step 4: 验证（需 kind/dev PG 在跑）**

Run: `make db-apply`
Expected: 末尾打印表数 ≥ 23（原 20 + channel_config + template + log；若 consent 之前缺则 +1）；无 `already exists` / `duplicate key` 错。

再跑一次 `make db-apply` → 仍 0 错（验证幂等）。
`docker exec apihub-pg psql -U apihub_app -d apihub -c "\dt" | grep -E "notification_channel_config|notification_template|notification_log|webhook_subscription"` → 4 行都在。

- [ ] **Step 5: Commit**

```bash
git add scripts/k8s/apply-db.sh Makefile scripts/kind/bootstrap.sh
git commit -m "feat(r2b): apply-db 幂等回放 init-db（make db-apply + bootstrap 集成）"
```

---

## Task 3: Channel 抽象 + EmailChannel + DingTalkChannel + Registry

**Files:**
- Modify: `services/services/notification/pyproject.toml`
- Create: `services/services/notification/src/notification/channels/{__init__,base,email,dingtalk,registry}.py`
- Test: `services/services/notification/tests/test_channels.py`

**Interfaces:**
- Produces: `Channel`（ABC, `async send(NotificationMessage)->SendResult`）、`NotificationMessage`、`SendResult`、`ChannelRegistry.get(type)`。下游 T6 routes 用。

- [ ] **Step 1: 加依赖**

`services/services/notification/pyproject.toml` 的 `dependencies` 加：
```toml
  "aiosmtplib>=3,<4",
  "jsonschema>=4,<5",
```

- [ ] **Step 2: 写 `channels/base.py`**

```python
"""Channel 抽象基类与消息/结果数据类。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class NotificationMessage:
    recipient: str
    subject: str
    body: str
    channel_type: str
    config: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)


@dataclass
class SendResult:
    success: bool
    error: str | None = None
    provider_msg_id: str | None = None


class Channel(ABC):
    channel_type: str = ""

    @abstractmethod
    async def send(self, message: NotificationMessage) -> SendResult:
        """发送。永不抛异常——失败返回 SendResult(success=False, error=...)。"""
```

- [ ] **Step 3: 写 `channels/email.py`**

```python
"""邮件渠道（aiosmtplib）。tenant 配置缺失时回退平台 env。"""

from __future__ import annotations

import os
from email.message import EmailMessage

import aiosmtplib

from notification.channels.base import Channel, NotificationMessage, SendResult


def _platform_smtp() -> dict:
    """平台默认 SMTP（env 未设则空 dict）。"""
    host = os.environ.get("NOTIFICATION_SMTP_HOST")
    if not host:
        return {}
    return {
        "smtp_host": host,
        "smtp_port": os.environ.get("NOTIFICATION_SMTP_PORT", "587"),
        "smtp_user": os.environ.get("NOTIFICATION_SMTP_USER", ""),
        "smtp_password": os.environ.get("NOTIFICATION_SMTP_PASSWORD", ""),
        "from_addr": os.environ.get("NOTIFICATION_SMTP_FROM_ADDR", ""),
        "use_tls": os.environ.get("NOTIFICATION_SMTP_USE_TLS", "false").lower()
        in ("1", "true", "yes"),
    }


class EmailChannel(Channel):
    channel_type = "email"

    async def send(self, message: NotificationMessage) -> SendResult:
        cfg = {**_platform_smtp(), **(message.config or {})}
        host = cfg.get("smtp_host")
        if not host:
            return SendResult(success=False, error="no smtp config")
        from_addr = cfg.get("from_addr") or message.recipient
        msg = EmailMessage()
        msg["From"] = from_addr
        msg["To"] = message.recipient
        msg["Subject"] = message.subject
        msg.set_content(message.body)
        try:
            async with aiosmtplib.SMTP(
                host, int(cfg.get("smtp_port", 587)),
                use_tls=bool(cfg.get("use_tls")), timeout=10,
            ) as smtp:
                user, pwd = cfg.get("smtp_user"), cfg.get("smtp_password")
                if user:
                    await smtp.login(user, pwd or "")
                await smtp.send_message(msg)
        except Exception as e:  # 网络/Auth 失败→业务结果，不抛
            return SendResult(success=False, error=str(e))
        return SendResult(success=True, provider_msg_id=None)
```

- [ ] **Step 4: 写 `channels/dingtalk.py`**

```python
"""钉钉自定义机器人渠道（HMAC-SHA256 加签）。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
import urllib.parse

import httpx

from notification.channels.base import Channel, NotificationMessage, SendResult


def _sign(timestamp_ms: int, secret: str) -> str:
    string_to_sign = f"{timestamp_ms}\n{secret}"
    digest = hmac.new(
        secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256
    ).digest()
    return urllib.parse.quote_plus(base64.b64encode(digest))


class DingTalkChannel(Channel):
    channel_type = "dingtalk"

    async def send(self, message: NotificationMessage) -> SendResult:
        cfg = message.config or {}
        webhook_url = cfg.get("webhook_url")
        if not webhook_url:
            return SendResult(success=False, error="no webhook_url")
        secret = cfg.get("secret") or ""
        url = webhook_url
        if secret:
            ts = int(time.time() * 1000)
            url = f"{webhook_url}&timestamp={ts}&sign={_sign(ts, secret)}"
        payload = {
            "msgtype": "markdown",
            "markdown": {"title": message.subject or "notification", "text": message.body},
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                resp = await c.post(url, json=payload)
            data = resp.json()
        except Exception as e:
            return SendResult(success=False, error=str(e))
        if data.get("errcode") != 0:
            return SendResult(success=False, error=str(data.get("errmsg", "dingtalk error")))
        return SendResult(success=True, provider_msg_id=data.get("msgid") or data.get("taskId"))
```

- [ ] **Step 5: 写 `channels/registry.py`**

```python
"""渠道注册表。"""

from __future__ import annotations

from notification.channels.base import Channel
from notification.channels.dingtalk import DingTalkChannel
from notification.channels.email import EmailChannel

_REGISTRY: dict[str, Channel] = {
    "email": EmailChannel(),
    "dingtalk": DingTalkChannel(),
}


def get(channel_type: str) -> Channel:
    ch = _REGISTRY.get(channel_type)
    if ch is None:
        from apihub_core.errors import ApiError, ErrorCode
        raise ApiError(ErrorCode.INVALID_INPUT, f"unsupported channel_type: {channel_type}")
    return ch


def _register(channel: Channel) -> None:
    _REGISTRY[channel.channel_type] = channel
```

- [ ] **Step 6: 写 `channels/__init__.py`**

```python
"""notification 渠道层。"""

from notification.channels.base import Channel, NotificationMessage, SendResult
from notification.channels.registry import get

__all__ = ["Channel", "NotificationMessage", "SendResult", "get"]
```

- [ ] **Step 7: 写测试 `tests/test_channels.py`**

```python
"""Channel 单测：Email（mock aiosmtplib）/ DingTalk（mock httpx，验签名）。"""

import base64
import hashlib
import hmac
import urllib.parse


# ---------- EmailChannel ----------

class _FakeSMTP:
    def __init__(self, *a, **kw):
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def login(self, user, pwd):
        self.calls.append(("login", user, pwd))

    async def send_message(self, msg):
        self.calls.append(("send_message", msg["From"], msg["To"], msg["Subject"]))


class TestEmailChannel:
    async def test_send_uses_config(self, monkeypatch):
        from notification.channels import email as email_mod

        fake = _FakeSMTP()
        monkeypatch.setattr(email_mod.aiosmtplib, "SMTP", lambda *a, **kw: fake)
        monkeypatch.delenv("NOTIFICATION_SMTP_HOST", raising=False)

        from notification.channels.base import NotificationMessage
        result = await email_mod.EmailChannel().send(NotificationMessage(
            recipient="a@b.com", subject="S", body="B", channel_type="email",
            config={"smtp_host": "mail", "smtp_port": "465", "smtp_user": "u",
                    "smtp_password": "p", "from_addr": "from@x.com", "use_tls": True},
        ))
        assert result.success is True
        assert fake.calls[0] == ("login", "u", "p")
        assert fake.calls[1][2] == "a@b.com"

    async def test_no_config_no_env_returns_failure(self, monkeypatch):
        from notification.channels import email as email_mod
        monkeypatch.delenv("NOTIFICATION_SMTP_HOST", raising=False)
        from notification.channels.base import NotificationMessage
        result = await email_mod.EmailChannel().send(NotificationMessage(
            recipient="a@b.com", subject="S", body="B", channel_type="email", config={}))
        assert result.success is False
        assert "no smtp config" in (result.error or "")

    async def test_platform_env_fallback(self, monkeypatch):
        from notification.channels import email as email_mod
        fake = _FakeSMTP()
        monkeypatch.setattr(email_mod.aiosmtplib, "SMTP", lambda *a, **kw: fake)
        monkeypatch.setenv("NOTIFICATION_SMTP_HOST", "platform-mail")
        monkeypatch.setenv("NOTIFICATION_SMTP_FROM_ADDR", "noreply@platform.com")
        from notification.channels.base import NotificationMessage
        result = await email_mod.EmailChannel().send(NotificationMessage(
            recipient="a@b.com", subject="S", body="B", channel_type="email", config={}))
        assert result.success is True  # tenant config 空→回退平台 env

    async def test_network_error_is_result_not_raise(self, monkeypatch):
        from notification.channels import email as email_mod

        class _Boom:
            async def __aenter__(self): return self
            async def __aexit__(self, *e): return False
            async def login(self, *a): pass
            async def send_message(self, m): raise ConnectionRefusedError("boom")

        monkeypatch.setattr(email_mod.aiosmtplib, "SMTP", lambda *a, **kw: _Boom())
        monkeypatch.delenv("NOTIFICATION_SMTP_HOST", raising=False)
        from notification.channels.base import NotificationMessage
        result = await email_mod.EmailChannel().send(NotificationMessage(
            recipient="a@b.com", subject="S", body="B", channel_type="email",
            config={"smtp_host": "mail"}))
        assert result.success is False
        assert "boom" in (result.error or "")


# ---------- DingTalkChannel ----------

class _FakeResp:
    def __init__(self, json_data): self._j = json_data
    def json(self): return self._j


class _FakeHttpxClient:
    def __init__(self, *a, **kw): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *e): return False
    async def post(self, url, json=None, timeout=None):
        self.url = url
        return _FakeResp({"errcode": 0, "msgid": "mid_123"})


class TestDingTalkChannel:
    async def test_signs_url_and_parses_success(self, monkeypatch):
        from notification.channels import dingtalk as dt_mod
        fake = _FakeHttpxClient()
        monkeypatch.setattr(dt_mod.httpx, "AsyncClient", lambda *a, **kw: fake)

        from notification.channels.base import NotificationMessage
        await dt_mod.DingTalkChannel().send(NotificationMessage(
            recipient="", subject="S", body="B", channel_type="dingtalk",
            config={"webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=T",
                    "secret": "SECtest"}))
        assert "timestamp=" in fake.url and "sign=" in fake.url
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(fake.url).query)
        ts = int(q["timestamp"][0]); got = q["sign"][0]
        string_to_sign = f"{ts}\nSECtest"
        exp = urllib.parse.quote_plus(base64.b64encode(
            hmac.new(b"SECtest", string_to_sign.encode(), hashlib.sha256).digest()))
        assert got == exp

    async def test_success_returns_provider_msg_id(self, monkeypatch):
        from notification.channels import dingtalk as dt_mod
        fake = _FakeHttpxClient()
        monkeypatch.setattr(dt_mod.httpx, "AsyncClient", lambda *a, **kw: fake)
        from notification.channels.base import NotificationMessage
        r = await dt_mod.DingTalkChannel().send(NotificationMessage(
            recipient="", subject="S", body="B", channel_type="dingtalk",
            config={"webhook_url": "https://x/y?access_token=T", "secret": "s"}))
        assert r.success is True and r.provider_msg_id == "mid_123"

    async def test_errcode_nonzero_is_failure(self, monkeypatch):
        from notification.channels import dingtalk as dt_mod

        class _Err(_FakeHttpxClient):
            async def post(self, url, json=None, timeout=None):
                self.url = url
                return _FakeResp({"errcode": 310000, "errmsg": "sign not match"})

        monkeypatch.setattr(dt_mod.httpx, "AsyncClient", lambda *a, **kw: _Err())
        from notification.channels.base import NotificationMessage
        r = await dt_mod.DingTalkChannel().send(NotificationMessage(
            recipient="", subject="S", body="B", channel_type="dingtalk",
            config={"webhook_url": "https://x/y?access_token=T", "secret": "s"}))
        assert r.success is False and "sign not match" in (r.error or "")

    async def test_no_webhook_url_failure(self):
        from notification.channels import dingtalk as dt_mod
        from notification.channels.base import NotificationMessage
        r = await dt_mod.DingTalkChannel().send(NotificationMessage(
            recipient="", subject="S", body="B", channel_type="dingtalk", config={}))
        assert r.success is False and "webhook_url" in (r.error or "")

    async def test_no_secret_posts_plain(self, monkeypatch):
        from notification.channels import dingtalk as dt_mod
        fake = _FakeHttpxClient()
        monkeypatch.setattr(dt_mod.httpx, "AsyncClient", lambda *a, **kw: fake)
        from notification.channels.base import NotificationMessage
        await dt_mod.DingTalkChannel().send(NotificationMessage(
            recipient="", subject="S", body="B", channel_type="dingtalk",
            config={"webhook_url": "https://x/y?access_token=T"}))  # 无 secret
        assert "sign=" not in fake.url  # 不签名


# ---------- registry ----------

class TestRegistry:
    async def test_get_known(self):
        from notification.channels import registry
        assert registry.get("email").channel_type == "email"
        assert registry.get("dingtalk").channel_type == "dingtalk"

    async def test_get_unknown_raises(self):
        import pytest
        from apihub_core.errors import ApiError
        from notification.channels import registry
        with pytest.raises(ApiError):
            registry.get("carrier_pigeon")
```

- [ ] **Step 8: 跑测试**

Run: `cd services/services/notification && python -m pytest tests/test_channels.py -v`
Expected: 全 PASS。

- [ ] **Step 9: Commit**

```bash
git add services/services/notification/pyproject.toml services/services/notification/src/notification/channels services/services/notification/tests/test_channels.py
git commit -m "feat(r2b): Channel 抽象 + Email(aiosmtplib)/DingTalk(HMAC) 渠道 + registry"
```

---

## Task 4: renderer（`{{var}}` + jsonschema + locale 回退）

**Files:**
- Create: `services/services/notification/src/notification/renderer.py`
- Test: `services/services/notification/tests/test_renderer.py`

**Interfaces:**
- Consumes: `notification_template` 表（经 caller 传入的 admin conn）。
- Produces: `async render(conn, *, code, channel_type, variables, locale) -> tuple[str, str]`。

- [ ] **Step 1: 写 `renderer.py`**

```python
"""模板渲染：locale 回退 + jsonschema 校验 + {{var}} 替换。"""

from __future__ import annotations

import re

import jsonschema
from apihub_core.errors import ApiError, ErrorCode

_VAR_RE = re.compile(r"\{\{(\w+)\}\}")
DEFAULT_LOCALE = "zh-CN"


async def _fetch_template(conn, *, code: str, channel_type: str, locale: str):
    """按 locale → 默认 → 任一 回退取模板行。"""
    for loc in (locale, DEFAULT_LOCALE):
        row = await conn.fetchrow(
            "SELECT subject_tpl, body_tpl, variables_schema FROM notification_template"
            " WHERE code = $1 AND channel_type = $2 AND locale = $3",
            code, channel_type, loc,
        )
        if row:
            return row
    row = await conn.fetchrow(
        "SELECT subject_tpl, body_tpl, variables_schema FROM notification_template"
        " WHERE code = $1 AND channel_type = $2 ORDER BY locale LIMIT 1",
        code, channel_type,
    )
    return row


def _substitute(tpl: str, variables: dict) -> str:
    def repl(m: re.Match) -> str:
        return str(variables.get(m.group(1), ""))
    return _VAR_RE.sub(repl, tpl)


async def render(conn, *, code: str, channel_type: str, variables: dict, locale: str) -> tuple[str, str]:
    row = await _fetch_template(conn, code=code, channel_type=channel_type, locale=locale)
    if not row:
        raise ApiError(ErrorCode.NOT_FOUND, f"template not found: {code}/{channel_type}")
    schema = row["variables_schema"] or {}
    if schema:
        try:
            jsonschema.validate(variables, schema)
        except jsonschema.ValidationError as e:
            raise ApiError(ErrorCode.INVALID_INPUT, f"invalid variables: {e.message}", http_status=400) from e
    subject = _substitute(row["subject_tpl"] or "", variables)
    body = _substitute(row["body_tpl"] or "", variables)
    return subject, body
```

- [ ] **Step 2: 写测试 `tests/test_renderer.py`**

```python
"""renderer 单测（_FakeConn admin 模式）。"""

import pytest
from apihub_core.errors import ApiError
from notification import renderer as renderer_mod


class _FakeRow:
    def __init__(self, d): self._d = d
    def __getitem__(self, k): return self._d[k]


class _FakeConn:
    def __init__(self, templates):
        self._templates = templates
    async def fetchrow(self, sql, *args):
        code, ctype = args[0], args[1]
        loc = args[2] if len(args) > 2 else None
        matches = [t for t in self._templates if t["code"] == code and t["channel_type"] == ctype]
        if not matches:
            return None
        if loc:
            for t in matches:
                if t["locale"] == loc:
                    return _FakeRow(t)
            return None
        return _FakeRow(matches[0])


class TestRender:
    async def test_substitutes_vars(self):
        conn = _FakeConn([{"code": "t", "channel_type": "email", "locale": "zh-CN",
                           "subject_tpl": "Hi {{name}}", "body_tpl": "ID={{id}}",
                           "variables_schema": {"type": "object", "required": ["name", "id"],
                                                "properties": {"name": {"type": "string"},
                                                               "id": {"type": "string"}}}}])
        subject, body = await renderer_mod.render(
            conn, code="t", channel_type="email",
            variables={"name": "Bob", "id": "x1"}, locale="zh-CN")
        assert subject == "Hi Bob" and body == "ID=x1"

    async def test_locale_fallback_to_default(self):
        conn = _FakeConn([{"code": "t", "channel_type": "email", "locale": "zh-CN",
                           "subject_tpl": "S", "body_tpl": "B", "variables_schema": {}}])
        subject, body = await renderer_mod.render(
            conn, code="t", channel_type="email", variables={}, locale="en")
        assert subject == "S" and body == "B"

    async def test_missing_var_renders_empty(self):
        conn = _FakeConn([{"code": "t", "channel_type": "email", "locale": "zh-CN",
                           "subject_tpl": "[{{a}}]", "body_tpl": "B", "variables_schema": {}}])
        s, _ = await renderer_mod.render(conn, code="t", channel_type="email", variables={}, locale="zh-CN")
        assert s == "[]"

    async def test_schema_validation_failure(self):
        conn = _FakeConn([{"code": "t", "channel_type": "email", "locale": "zh-CN",
                           "subject_tpl": "S", "body_tpl": "B",
                           "variables_schema": {"type": "object", "required": ["name"],
                                                "properties": {"name": {"type": "string"}}}}])
        with pytest.raises(ApiError):
            await renderer_mod.render(conn, code="t", channel_type="email",
                                      variables={}, locale="zh-CN")

    async def test_template_not_found(self):
        conn = _FakeConn([])
        with pytest.raises(ApiError):
            await renderer_mod.render(conn, code="nope", channel_type="email",
                                      variables={}, locale="zh-CN")
```

- [ ] **Step 3: 跑测试**

Run: `cd services/services/notification && python -m pytest tests/test_renderer.py -v`
Expected: 全 PASS。

- [ ] **Step 4: Commit**

```bash
git add services/services/notification/src/notification/renderer.py services/services/notification/tests/test_renderer.py
git commit -m "feat(r2b): renderer（{{var}} 替换 + jsonschema 校验 + locale 回退）"
```

---

## Task 5: repository 增（channel_config CRUD + log + render_template 包装）

**Files:**
- Modify: `services/services/notification/src/notification/repository.py`

**Interfaces:**
- Consumes: `db.db_session()`（channel_config/log）、`db.admin_db_session()`（template）。
- Produces: `list_channel_configs`、`create_channel_config`、`update_channel_config`、`delete_channel_config`、`get_active_channel_config`、`insert_notification_log`、`render_template`。下游 T6 routes 用。

- [ ] **Step 1: 在 `repository.py` 追加**

确认顶部已有 `import secrets`、`from apihub_core import db`、`from apihub_core.errors import ApiError, ErrorCode`（webhook 段已 import；`secrets` 在 `create_webhook` 用过，已存在）。在文件末尾追加：

```python
from notification import renderer as renderer_mod


async def list_channel_configs(*, tenant_id: str) -> list[dict]:
    async with db.db_session() as conn:
        rows = await conn.fetch(
            "SELECT id, channel_type, name, config, status, created_at"
            " FROM notification_channel_config WHERE tenant_id = $1 ORDER BY created_at DESC",
            tenant_id,
        )
    return [dict(r) for r in rows]


async def create_channel_config(*, tenant_id: str, channel_type: str, name: str,
                                config: dict, status: str) -> dict:
    cc_id = f"cc_{secrets.token_hex(8)}"
    async with db.db_session() as conn:
        await conn.execute(
            "INSERT INTO notification_channel_config (id, tenant_id, channel_type, name, config, status)"
            " VALUES ($1, $2, $3, $4, $5, $6)",
            cc_id, tenant_id, channel_type, name, config, status or "active",
        )
    return {"id": cc_id, "channel_type": channel_type, "name": name, "config": config, "status": status or "active"}


async def update_channel_config(*, tenant_id: str, config_id: str, updates: dict) -> dict:
    if not updates:
        raise ApiError(ErrorCode.INVALID_INPUT, "no fields to update", http_status=400)
    sets = ", ".join(f"{k} = ${i+2}" for i, k in enumerate(updates))
    values = list(updates.values())
    async with db.db_session() as conn:
        row = await conn.fetchrow(
            f"UPDATE notification_channel_config SET {sets} WHERE id = $1 AND tenant_id = ${len(values)+2}"
            " RETURNING id, channel_type, name, config, status, created_at",
            config_id, *values, tenant_id,
        )
    if not row:
        raise ApiError(ErrorCode.NOT_FOUND, "channel config not found")
    return dict(row)


async def delete_channel_config(*, tenant_id: str, config_id: str) -> None:
    async with db.db_session() as conn:
        result = await conn.execute(
            "DELETE FROM notification_channel_config WHERE id = $1 AND tenant_id = $2",
            config_id, tenant_id,
        )
    if "DELETE 0" in result:
        raise ApiError(ErrorCode.NOT_FOUND, "channel config not found")


async def get_active_channel_config(*, tenant_id: str, channel_type: str) -> dict | None:
    """send 时解析 tenant 的 active 渠道配置；无则 None（email 由 caller/env 回退，dingtalk 无则 send 返失败）。"""
    async with db.db_session() as conn:
        row = await conn.fetchrow(
            "SELECT config FROM notification_channel_config"
            " WHERE tenant_id = $1 AND channel_type = $2 AND status = 'active'",
            tenant_id, channel_type,
        )
    return dict(row["config"]) if row else None


async def insert_notification_log(*, tenant_id: str, template_code: str, channel_type: str,
                                  recipient: str, status: str, error: str, provider_msg_id: str) -> None:
    log_id = f"nl_{secrets.token_hex(8)}"
    async with db.db_session() as conn:
        await conn.execute(
            "INSERT INTO notification_log (id, tenant_id, template_code, channel_type, recipient,"
            " status, error, provider_msg_id) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
            log_id, tenant_id, template_code, channel_type, recipient, status, error, provider_msg_id,
        )


async def render_template(*, code: str, channel_type: str, variables: dict, locale: str) -> tuple[str, str]:
    """admin 读模板并渲染（routes 经此调用，便于测试 monkeypatch）。"""
    async with db.admin_db_session() as conn:
        return await renderer_mod.render(conn, code=code, channel_type=channel_type,
                                         variables=variables, locale=locale)
```

- [ ] **Step 2: 跑既有测试不回归**

Run: `cd services/services/notification && python -m pytest tests/ -v`
Expected: 既有 webhook/consumer 测试仍 PASS（新函数未接线进路由，不回归）。

- [ ] **Step 3: Commit**

```bash
git add services/services/notification/src/notification/repository.py
git commit -m "feat(r2b): repository 增 channel_config CRUD / insert_log / render_template"
```

---

## Task 6: routes + models（channel-configs CRUD + notify/send + batch）

**Files:**
- Modify: `services/services/notification/src/notification/models.py`、`services/services/notification/src/notification/routes.py`
- Test: `services/services/notification/tests/test_routes.py`（追加）

**Interfaces:**
- Consumes: T3 registry（`channels.get`/`channels.NotificationMessage`）、T5 repository（`get_active_channel_config`/`render_template`/`insert_notification_log` + channel_config CRUD）。
- Produces: 5 个新端点。

- [ ] **Step 1: `models.py` 追加**

```python
class NotifyRequest(BaseModel):
    template_code: str
    channel_type: str
    recipient: str = ""
    variables: dict = Field(default_factory=dict)
    locale: str = "zh-CN"


class NotifyResult(BaseModel):
    success: bool
    error: str | None = None
    provider_msg_id: str | None = None


class ChannelConfigCreate(BaseModel):
    channel_type: str
    name: str = "default"
    config: dict
    status: str = "active"


class ChannelConfigUpdate(BaseModel):
    channel_type: str | None = None
    name: str | None = None
    config: dict | None = None
    status: str | None = None


class ChannelConfigResponse(BaseModel):
    id: str
    channel_type: str
    name: str
    config: dict
    status: str
```

- [ ] **Step 2: `routes.py` 追加端点**

文件顶部 import 增：`from notification import channels`；扩展 models import 含 `NotifyRequest, ChannelConfigCreate, ChannelConfigUpdate`。

在 `register_routes` 内（现有 webhook 路由之后）追加：

```python
    # ===== /v1/notification/channel-configs（per-tenant CRUD）=====
    @app.get("/v1/notification/channel-configs")
    async def list_channel_configs():
        ctx = require_tenant()
        return await repository.list_channel_configs(tenant_id=ctx.tenant_id)

    @app.post("/v1/notification/channel-configs", status_code=201)
    async def create_channel_config(payload: ChannelConfigCreate):
        ctx = require_tenant()
        return await repository.create_channel_config(
            tenant_id=ctx.tenant_id, channel_type=payload.channel_type,
            name=payload.name, config=payload.config, status=payload.status,
        )

    @app.put("/v1/notification/channel-configs/{config_id}")
    async def update_channel_config(config_id: str, payload: ChannelConfigUpdate):
        ctx = require_tenant()
        return await repository.update_channel_config(
            tenant_id=ctx.tenant_id, config_id=config_id,
            updates=payload.model_dump(exclude_none=True),
        )

    @app.delete("/v1/notification/channel-configs/{config_id}")
    async def delete_channel_config(config_id: str):
        ctx = require_tenant()
        await repository.delete_channel_config(tenant_id=ctx.tenant_id, config_id=config_id)
        return {"status": "deleted"}

    # ===== /v1/internal/notify/send + /batch =====
    async def _handle_one(tenant_id: str, req: NotifyRequest) -> dict:
        config = await repository.get_active_channel_config(
            tenant_id=tenant_id, channel_type=req.channel_type)
        subject, body = await repository.render_template(
            code=req.template_code, channel_type=req.channel_type,
            variables=req.variables, locale=req.locale)
        channel = channels.get(req.channel_type)
        result = await channel.send(channels.NotificationMessage(
            recipient=req.recipient, subject=subject, body=body,
            channel_type=req.channel_type, config=config or {},
            meta={"template_code": req.template_code}))
        await repository.insert_notification_log(
            tenant_id=tenant_id, template_code=req.template_code,
            channel_type=req.channel_type, recipient=req.recipient,
            status="sent" if result.success else "failed",
            error=result.error or "", provider_msg_id=result.provider_msg_id or "")
        return {"success": result.success, "error": result.error, "provider_msg_id": result.provider_msg_id}

    @app.post("/v1/internal/notify/send")
    async def notify_send(payload: NotifyRequest):
        ctx = require_tenant()
        return await _handle_one(ctx.tenant_id, payload)

    @app.post("/v1/internal/notify/batch")
    async def notify_batch(payload: list[NotifyRequest]):
        import asyncio
        ctx = require_tenant()
        results = await asyncio.gather(
            *[_handle_one(ctx.tenant_id, r) for r in payload], return_exceptions=True)
        out = []
        for r in results:
            if isinstance(r, Exception):
                out.append({"success": False, "error": str(r), "provider_msg_id": None})
            else:
                out.append(r)
        return out
```
> 注：`channels.NotificationMessage`——`notification/channels/__init__.py` 已 `__all__` 暴露。顶部 `from notification import channels`。

- [ ] **Step 3: 追加路由测试（`tests/test_routes.py` 末尾）**

```python
from notification import channels as channels_mod
from notification.channels.base import NotificationMessage, SendResult


class _FakeChannel:
    async def send(self, message: NotificationMessage) -> SendResult:
        self.last = message
        return SendResult(success=True, provider_msg_id="mid_test")


class TestChannelConfigs:
    async def test_create_then_list(self, client, monkeypatch):
        created = {}
        async def _create(*, tenant_id, channel_type, name, config, status):
            created["t"] = tenant_id
            return {"id": "cc_1", "channel_type": channel_type, "name": name,
                    "config": config, "status": status}
        monkeypatch.setattr(repo_mod, "create_channel_config", _create)
        r = await client.post("/v1/notification/channel-configs",
            json={"channel_type": "dingtalk",
                  "config": {"webhook_url": "https://x/y?access_token=T", "secret": "s"}})
        assert r.status_code == 201 and r.json()["id"] == "cc_1"
        assert created["t"] == "t_default"

    async def test_delete(self, client, monkeypatch):
        captured = {}
        async def _delete(*, tenant_id, config_id):
            captured["id"] = config_id
        monkeypatch.setattr(repo_mod, "delete_channel_config", _delete)
        r = await client.delete("/v1/notification/channel-configs/cc_1")
        assert r.status_code == 200 and captured["id"] == "cc_1"


class TestNotifySend:
    async def _setup(self, monkeypatch):
        fake = _FakeChannel()
        monkeypatch.setattr(channels_mod, "get", lambda t: fake)
        async def _cfg(*, tenant_id, channel_type):
            return {"webhook_url": "https://x/y?access_token=T", "secret": "s"}
        async def _render(*, code, channel_type, variables, locale):
            return (" subj ", " body ")
        log = []
        async def _log(*, tenant_id, template_code, channel_type, recipient, status, error, provider_msg_id):
            log.append({"status": status, "recipient": recipient, "error": error})
        monkeypatch.setattr(repo_mod, "get_active_channel_config", _cfg)
        monkeypatch.setattr(repo_mod, "render_template", _render)
        monkeypatch.setattr(repo_mod, "insert_notification_log", _log)
        return fake, log

    async def test_send_success_writes_sent_log(self, client, monkeypatch):
        _fake, log = await self._setup(monkeypatch)
        r = await client.post("/v1/internal/notify/send", json={
            "template_code": "task_complete", "channel_type": "dingtalk",
            "variables": {"task_id": "t1", "task_name": "N"}, "locale": "zh-CN"})
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is True and body["provider_msg_id"] == "mid_test"
        assert log and log[0]["status"] == "sent"

    async def test_send_failure_writes_failed_log(self, client, monkeypatch):
        _fake, log = await self._setup(monkeypatch)
        async def _bad(self, message): return SendResult(success=False, error="boom")
        monkeypatch.setattr(_FakeChannel, "send", _bad)
        r = await client.post("/v1/internal/notify/send", json={
            "template_code": "task_complete", "channel_type": "dingtalk",
            "variables": {"task_id": "t1", "task_name": "N"}})
        assert r.json()["success"] is False
        assert log[0]["status"] == "failed" and log[0]["error"] == "boom"

    async def test_batch_returns_list(self, client, monkeypatch):
        await self._setup(monkeypatch)
        r = await client.post("/v1/internal/notify/batch", json=[
            {"template_code": "task_complete", "channel_type": "dingtalk",
             "variables": {"task_id": "a", "task_name": "A"}},
            {"template_code": "task_complete", "channel_type": "dingtalk",
             "variables": {"task_id": "b", "task_name": "B"}},
        ])
        assert r.status_code == 200 and len(r.json()) == 2
        assert all(x["success"] for x in r.json())
```

- [ ] **Step 4: 跑全量测试 + lint**

Run:
```bash
cd services/services/notification && python -m pytest tests/ -v
cd /home/applo/project/ai-system-arch && ruff check services/services/notification && mypy services/services/notification
```
Expected: 全 PASS / 0 lint 错。

- [ ] **Step 5: Commit**

```bash
git add services/services/notification/src/notification/models.py services/services/notification/src/notification/routes.py services/services/notification/tests/test_routes.py
git commit -m "feat(r2b): routes — channel-configs CRUD + /internal/notify/send + /batch"
```

---

## Task 7: kind e2e 验证

**Files:** 无新代码（部署 + 真实入口验证）。

**Interfaces:**
- Consumes: T1–T6 全部。

- [ ] **Step 1: 重建 kind 栈（含 apply-db）**

Run:
```bash
bash scripts/kind/bootstrap.sh 2>&1 | tail -40
```
Expected: `apply-db` 段无 `already exists`/`duplicate key` 错；表数 ≥ 23；pod 全 `Running`。

- [ ] **Step 2: 确认表 + seed**

Run:
```bash
docker exec apihub-pg psql -U apihub_app -d apihub -c "\dt" | grep -E "notification_channel_config|notification_template|notification_log|webhook_subscription|user_consent"
docker exec apihub-pg psql -U apihub_app -d apihub -tAc "SELECT count(*) FROM notification_template;"
```
Expected: 5 表都在；template ≥ 4 行。

- [ ] **Step 3: 转发 notification + 注册钉钉配置 + 发送**

```bash
kubectl -n apihub-system port-forward svc/notification 18012:80 &
sleep 3
AK=$(grep -oP "ak_test_a_\w+" scripts/init-db/02-seed.sql | head -1)
curl -sf -X POST http://127.0.0.1:18012/v1/notification/channel-configs \
  -H "X-API-Key: $AK" -H "Content-Type: application/json" \
  -d '{"channel_type":"dingtalk","config":{"webhook_url":"https://oapi.dingtalk.com/robot/send?access_token=mock","secret":"SECmock"}}'
curl -sf -X POST http://127.0.0.1:18012/v1/internal/notify/send \
  -H "X-API-Key: $AK" -H "Content-Type: application/json" \
  -d '{"template_code":"task_complete","channel_type":"dingtalk","variables":{"task_id":"t1","task_name":"测试任务"},"locale":"zh-CN"}'
docker exec apihub-pg psql -U apihub_app -d apihub -c "SELECT status,error FROM notification_log ORDER BY created_at DESC LIMIT 3;"
```
Expected: channel-configs POST 201；send 返 `{"success":false,...}`（mock token 钉钉返错，符合预期）；`notification_log` 落 1 行 `failed`。**关键**：send 全链路通（配置解析→渲染→渠道调用→log），钉钉真错误被正确捕获成业务结果。

- [ ] **Step 4: batch + webhook 不回归**

```bash
curl -sf -X POST http://127.0.0.1:18012/v1/internal/notify/batch -H "X-API-Key: $AK" -H "Content-Type: application/json" \
  -d '[{"template_code":"task_complete","channel_type":"dingtalk","variables":{"task_id":"a","task_name":"A"}},{"template_code":"task_complete","channel_type":"dingtalk","variables":{"task_id":"b","task_name":"B"}}]'
docker exec apihub-pg psql -U apihub_app -d apihub -tAc "SELECT count(*) FROM notification_log;"
kubectl -n apihub-system logs deploy/notification --tail=30 | grep -i "error\|traceback" || echo "(no traceback)"
```
Expected: batch 返 list 长 2；log 行数累计 == 已发送数；notification consumer 无 traceback（webhook 不回归）。

- [ ] **Step 5: 收尾 commit（若有 e2e 期间小修）**

```bash
git status --short
# git add ... && git commit -m "fix(r2b): e2e 期间小修（描述）"
```

---

## Self-Review（plan 自检）

1. **Spec 覆盖**：Channel 抽象(T3)✓ 邮件/钉钉(T3)✓ `/send`+`/batch`(T6)✓ 模板系统(T1 seed + T4 render)✓ 投递日志(T1 表 + T5/T6 写)✓ channel_config per-tenant(T1 表 + T5/T6 CRUD)✓ kind schema-apply(T1 幂等 + T2 apply-db)✓。
2. **占位符**：无 TBD/TODO；代码块完整。
3. **类型一致**：`NotificationMessage`/`SendResult` 在 base 定义、channels/__init__ 暴露、routes/test 引用一致；`render(conn,...)` 签名 T4 定义、T5 `render_template` 转发、T6 调用一致；`get_active_channel_config`/`insert_notification_log` T5 定义、T6 调用一致。
4. **新依赖**：aiosmtplib/jsonschema 仅入 notification pyproject（不入 apihub-core）。
5. **幂等硬约束**：T1 显式处理 01/02/05 非幂等，T2 Step4 双跑验证。

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-17-r2b-notification-channels.md`. Two execution options:

1. **Subagent-Driven (recommended)** — 每任务派新 subagent + task-reviewer，迭代快。
2. **Inline Execution** — 本会话内分批执行带 checkpoint。

Which approach?
