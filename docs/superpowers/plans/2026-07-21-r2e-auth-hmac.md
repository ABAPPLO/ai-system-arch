# R2e — auth HMAC Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 补实现 HMAC 请求签名验签（inbound 客户端签/平台验 + outbound webhook 平台签/客户端验），envelope 加密存 secret，secret 可轮换。

**Architecture:** HMAC 是叠加在 `api_key` 之上的 opt-in 签名层（`hmac_secret_encrypted` 列非空 = 该 key 走签名模式）。inbound 在 `apihub_core` in-app 验签（warm=Redis 加密 blob + in-process decrypt；cold=auth `/v1/internal/hmac-secret`）。outbound 在 notification consumer 复用 signing 模块。secret 用 AESGCM envelope 加密存 PG（env `HMAC_SECRET_KEY` 独立，不复用 `AI_GATEWAY_ENCRYPTION_KEY`）。

**Tech Stack:** Python 3.11 / FastAPI / asyncpg / Redis / `cryptography` (AESGCM) / pytest (asyncio_mode=auto)

**Spec:** `docs/superpowers/specs/2026-07-21-r2e-auth-hmac-design.md`

## Global Constraints

- **Python 3.11**（`services/services/*/Dockerfile` base = `python:3.11-slim`，R0a #49 已统一）。
- **asyncpg 直连，不用 SQLAlchemy**；jsonb codec 已注册（dict 直传）。
- **多租户 + RLS 不变量**：`db_session()` 注入 `SET LOCAL app.tenant_id`；`admin_db_session()` bypass RLS（平台 admin 跨租户）+ 写 audit_log。secret 取用 `admin_db_session`（同 `verify_api_key_record`）。
- **PG superuser = `apihub`**（不可改，否则 RLS 失效）。apply-db 须 as owner `apihub`。
- **secret 不可存单向 hash**——HMAC 验签需真实 secret 字节重算后 `compare_digest`，故 envelope 加密（可逆）。
- **canonical 串**（inbound）= `f"{method}\n{raw_path_with_query}\n{timestamp}\n{sha256(body).hexdigest()}"`；query 保持 client wire 原样（percent-encoded，不 normalize/re-encode）。
- **常时比对**：所有签名比对走 `hmac.compare_digest`。
- **fail-closed**：缺 `HMAC_SECRET_KEY` → 启动期 RuntimeError（不进请求路径）。
- **enrolled key 必须签名**（不带签名头 → 401，防降级绕过 bearer）。
- **测试约定**：`asyncio_mode=auto`；conftest 注最小 env before import apihub_core；`reset_tenant_context` + `get_settings.cache_clear()` autouse；DB 测试 stub PG，端到端走 kind e2e。
- **lint**：ruff (E/F/I/B/UP/SIM/C4/ASYNC/S) + mypy 非严格，root `pyproject.toml`。
- **每轮一个 squash-PR**；push/merge 仅在用户要求时。

---

## File Structure

| 文件 | 责任 | 动作 |
|---|---|---|
| `services/libs/apihub-core/src/apihub_core/crypto.py` | AESGCM secret 加解密 | Create |
| `services/libs/apihub-core/src/apihub_core/signing.py` | HMAC canonical/sign/verify 纯函数（inbound + outbound） | Create |
| `services/libs/apihub-core/src/apihub_core/identity.py` | identity 缓存 + secret 缓存读写 | Modify |
| `services/libs/apihub-core/src/apihub_core/auth.py` | `_verify_hmac` + 挂入 `authenticate_request` | Modify |
| `services/libs/apihub-core/src/apihub_core/config.py` | Settings 字段 | Modify |
| `services/libs/apihub-core/tests/test_crypto.py` | crypto 单测 | Create |
| `services/libs/apihub-core/tests/test_signing.py` | signing 单测 | Create |
| `services/libs/apihub-core/tests/test_identity_hmac.py` | identity secret 缓存单测 | Create |
| `services/libs/apihub-core/tests/test_hmac_verify.py` | inbound 验签编排单测 | Create |
| `services/services/auth/src/auth/repository.py` | `create_api_key(signing=)`/`get_hmac_secret_plaintext`/`rotate_hmac_secret` | Modify |
| `services/services/auth/src/auth/routes.py` | create-with-signing / rotate / `/v1/internal/hmac-secret` | Modify |
| `services/services/auth/src/auth/models.py` | `ApiKeyCreate.signing` / `ApiKeyResponse.hmac_secret` / `HmacSecretRequest/Response` | Modify |
| `services/services/auth/src/auth/main.py` | `skip_auth_paths` 加 `/v1/internal/hmac-secret` | Modify |
| `services/services/auth/tests/test_hmac_repo.py` | auth repository HMAC 单测 | Create |
| `services/services/auth/tests/test_hmac_routes.py` | auth HMAC 端点单测 | Create |
| `services/services/notification/src/notification/consumer.py` | `_deliver` 用 `signing.sign_webhook` + 头格式 | Modify |
| `services/services/notification/src/notification/repository.py` | `create_webhook` 平台生成 secret + 加密存 | Modify |
| `services/services/notification/src/notification/models.py` | `WebhookResponse.hmac_secret` | Modify |
| `services/services/notification/tests/test_outbound_signing.py` | outbound 签名单测 | Create |
| `scripts/init-db/14-hmac-secret.sql` | ADD 列（api_key.hmac_secret_encrypted + webhook_subscription.secret_encrypted） | Create |
| `scripts/init-db/14-backfill-webhook-secret.py` | 存量明文 secret 加密回填 + scrub | Create |
| `docs/hmac-signing.md` | canonical + headers + Python/curl 示例 | Create |
| `.env.dev` | `HMAC_SECRET_KEY` | Modify |
| `deploy/k8s/services/auth/deployment.yaml` | env `HMAC_SECRET_KEY` | Modify |
| `deploy/k8s/services/dispatcher/deployment.yaml` | env `HMAC_SECRET_KEY` | Modify |
| `deploy/k8s/services/notification/deployment.yaml` | env `HMAC_SECRET_KEY` | Modify |

---

## Task 1: `apihub_core/crypto.py` + Settings env

**Files:**
- Create: `services/libs/apihub-core/src/apihub_core/crypto.py`
- Modify: `services/libs/apihub-core/src/apihub_core/config.py:97-149`
- Modify: `.env.dev`
- Test: `services/libs/apihub-core/tests/test_crypto.py`

**Interfaces:**
- Consumes: `apihub_core.config.get_settings()` (Settings.hmac_secret_key)
- Produces: `crypto.encrypt_secret(plaintext: str) -> str` (b64), `crypto.decrypt_secret(ciphertext_b64: str) -> str`

- [ ] **Step 1: Write failing test** — `services/libs/apihub-core/tests/test_crypto.py`

```python
"""AESGCM secret 加解密单测。"""

import pytest


@pytest.fixture(autouse=True)
def _set_key(monkeypatch):
    monkeypatch.setenv("HMAC_SECRET_KEY", "a" * 64)  # 32-byte hex
    from apihub_core.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_round_trip():
    from apihub_core.crypto import encrypt_secret, decrypt_secret
    ct = encrypt_secret("ak_supersecret_value")
    assert decrypt_secret(ct) == "ak_supersecret_value"


def test_ciphertext_nondeterministic():
    from apihub_core.crypto import encrypt_secret
    a = encrypt_secret("same_secret")
    b = encrypt_secret("same_secret")
    assert a != b  # AESGCM nonce 随机


def test_missing_key_raises(monkeypatch):
    monkeypatch.setenv("HMAC_SECRET_KEY", "")
    from apihub_core.config import get_settings
    get_settings.cache_clear()
    from apihub_core.crypto import encrypt_secret
    with pytest.raises(RuntimeError, match="HMAC_SECRET_KEY not configured"):
        encrypt_secret("x")


def test_tampered_ciphertext_raises():
    from apihub_core.crypto import encrypt_secret, decrypt_secret
    import base64
    ct = encrypt_secret("secret")
    raw = bytearray(base64.b64decode(ct))
    raw[-1] ^= 0xFF  # flip a byte in tag
    tampered = base64.b64encode(bytes(raw)).decode()
    with pytest.raises(Exception):  # cryptography.exceptions.InvalidTag
        decrypt_secret(tampered)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest services/libs/apihub-core/tests/test_crypto.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'apihub_core.crypto'`

- [ ] **Step 3: Implement crypto.py** — mirror `ai_gateway/crypto.py`

```python
"""AES-256-GCM 加解密 —— HMAC signing secret 加密存储。

密钥来源：环境变量 HMAC_SECRET_KEY（32 字节 hex 字符串）。
密文格式：base64(nonce + ciphertext + tag)。
与 ai_gateway/crypto.py 同构但独立 env key（爆炸半径隔离）。
"""

import base64
import os

from apihub_core.config import get_settings
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_LENGTH = 12  # AES-GCM 推荐 96-bit nonce


def _get_key() -> bytes:
    key_hex = get_settings().hmac_secret_key
    if not key_hex:
        raise RuntimeError("HMAC_SECRET_KEY not configured")
    return bytes.fromhex(key_hex)


def encrypt_secret(plaintext: str) -> str:
    """加密明文 → base64(nonce + ciphertext + tag)。"""
    key = _get_key()
    aesgcm = AESGCM(key)
    nonce = os.urandom(_NONCE_LENGTH)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(nonce + ciphertext).decode("ascii")


def decrypt_secret(ciphertext_b64: str) -> str:
    """解密 base64(nonce + ciphertext + tag) → 明文。损坏抛 InvalidTag（上层转 503/401）。"""
    key = _get_key()
    raw = base64.b64decode(ciphertext_b64)
    nonce = raw[:_NONCE_LENGTH]
    ct = raw[_NONCE_LENGTH:]
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ct, None).decode("utf-8")
```

- [ ] **Step 4: Add Settings fields** — `config.py` after `ai_gateway_encryption_key` (line ~97):

```python
    # HMAC 签名密钥加密 key（AES-256-GCM，32 字节 hex）——独立于 ai_gateway_encryption_key
    hmac_secret_key: str = ""
    # HMAC 请求验签参数
    hmac_timestamp_window_seconds: int = 300  # ±5min
    hmac_nonce_ttl_seconds: int = 600  # 10min
    # auth HMAC secret 冷路径（dispatcher 取 HMAC secret 用）
    hmac_secret_service_url: str = "http://auth.apihub-system/v1/internal/hmac-secret"
```

Add `"hmac_secret_key": ""` to `_INSECURE_DEFAULTS`:

```python
_INSECURE_DEFAULTS = {
    "jwt_secret": "dev-only-insecure-secret",
    "pii_encryption_key": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
    "oss_secret_key": "apihub_dev_pwd",
    "hmac_secret_key": "",  # prod 必须注入，缺则启动 fail-closed
}
```

- [ ] **Step 5: Add `.env.dev`**

```
HMAC_SECRET_KEY=deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef
```

- [ ] **Step 6: Run tests, verify pass**

Run: `pytest services/libs/apihub-core/tests/test_crypto.py -v`
Expected: PASS 4/4

- [ ] **Step 7: Commit**

```bash
git add services/libs/apihub-core/src/apihub_core/crypto.py services/libs/apihub-core/src/apihub_core/config.py services/libs/apihub-core/tests/test_crypto.py .env.dev
git commit -m "R2e T1: apihub_core.crypto (AESGCM secret 加解密) + Settings 字段"
```

---

## Task 2: `apihub_core/signing.py`（inbound + outbound 纯函数）

**Files:**
- Create: `services/libs/apihub-core/src/apihub_core/signing.py`
- Test: `services/libs/apihub-core/tests/test_signing.py`

**Interfaces:**
- Consumes: 无（纯函数）
- Produces:
  - `canonical_string(method: str, raw_path_with_query: str, body: bytes, timestamp: str) -> str`
  - `sign(secret: str, method: str, raw_path_with_query: str, body: bytes, timestamp: str) -> str`（inbound，hex）
  - `verify(secret: str, method: str, raw_path_with_query: str, body: bytes, timestamp: str, provided: str) -> bool`（inbound，compare_digest）
  - `sign_webhook(secret: str, body: bytes) -> str`（outbound，hex）
  - `verify_webhook(secret: str, body: bytes, provided: str) -> bool`（outbound）

> **Spec refinement:** spec §3.1 C5 说 outbound "复用 C2 sign()"，但 inbound canonical 含 method/path/timestamp，outbound webhook 是 body-only canonical。本任务把 C2 拆成 inbound `sign`/`verify` + outbound `sign_webhook`/`verify_webhook`，共用 `hmac.compare_digest` 常时比对。outbound `sign_webhook` = `HMAC-SHA256(secret, body)` 与既有 consumer.py 逐字节兼容（迁移零中断）。

- [ ] **Step 1: Write failing test** — `services/libs/apihub-core/tests/test_signing.py`

```python
"""HMAC signing 纯函数单测。"""

import hashlib
import hmac as _std_hmac


def test_canonical_string_shape():
    from apihub_core.signing import canonical_string
    body = b'{"x":1}'
    s = canonical_string("POST", "/v1/foo?a=1", body, "1700000000")
    assert s == f"POST\n/v1/foo?a=1\n1700000000\n{hashlib.sha256(body).hexdigest()}"


def test_canonical_body_field_isolation():
    from apihub_core.signing import canonical_string
    a = canonical_string("POST", "/p", b'{"x":1}', "1")
    b = canonical_string("POST", "/p", b'{"x":2}', "1")
    assert a != b


def test_sign_verify_roundtrip_inbound():
    from apihub_core.signing import sign, verify
    sig = sign("secret", "POST", "/v1/foo?a=1", b'{"x":1}', "1700000000")
    assert verify("secret", "POST", "/v1/foo?a=1", b'{"x":1}', "1700000000", sig) is True
    assert verify("secret", "POST", "/v1/foo?a=1", b'{"x":2}', "1700000000", sig) is False


def test_verify_wrong_secret_false():
    from apihub_core.signing import sign, verify
    sig = sign("secret", "POST", "/p", b"b", "1")
    assert verify("other", "POST", "/p", b"b", "1", sig) is False


def test_verify_truncated_signature_false():
    from apihub_core.signing import verify
    assert verify("s", "POST", "/p", b"b", "1", "short") is False


def test_empty_body():
    from apihub_core.signing import canonical_string
    s = canonical_string("GET", "/p", b"", "1")
    assert hashlib.sha256(b"").hexdigest() in s


def test_webhook_sign_verify():
    from apihub_core.signing import sign_webhook, verify_webhook
    body = b'{"event":"x"}'
    sig = sign_webhook("wh_secret", body)
    assert verify_webhook("wh_secret", body, sig) is True
    assert verify_webhook("wh_secret", b'{"event":"y"}', sig) is False


def test_webhook_matches_raw_hmac():
    """outbound 须与现有 consumer.py 的 hmac.new(secret, body, sha256) 逐字节兼容。"""
    from apihub_core.signing import sign_webhook
    body = b'{"event":"x"}'
    expected = _std_hmac.new(b"wh_secret", body, hashlib.sha256).hexdigest()
    assert sign_webhook("wh_secret", body) == expected
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest services/libs/apihub-core/tests/test_signing.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'apihub_core.signing'`

- [ ] **Step 3: Implement signing.py**

```python
"""HMAC 签名纯函数 —— inbound 请求验签 + outbound webhook 签名，单一真相源。

inbound canonical（§7.3）：
    canonical = f"{method}\n{raw_path_with_query}\n{timestamp}\n{sha256(body).hexdigest()}"
    signature = HMAC-SHA256(secret, canonical).hexdigest()

outbound（webhook body 签名，client 验）：
    signature = HMAC-SHA256(secret, body).hexdigest()   # body-only canonical

query 保持 client wire 原样（percent-encoded，不 normalize/re-encode）。
所有比对走 hmac.compare_digest（常时）。
"""

import hashlib
import hmac


def canonical_string(method: str, raw_path_with_query: str, body: bytes, timestamp: str) -> str:
    return f"{method}\n{raw_path_with_query}\n{timestamp}\n{hashlib.sha256(body).hexdigest()}"


def sign(secret: str, method: str, raw_path_with_query: str, body: bytes, timestamp: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        canonical_string(method, raw_path_with_query, body, timestamp).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify(
    secret: str,
    method: str,
    raw_path_with_query: str,
    body: bytes,
    timestamp: str,
    provided: str,
) -> bool:
    expected = sign(secret, method, raw_path_with_query, body, timestamp)
    return hmac.compare_digest(expected, provided)


def sign_webhook(secret: str, body: bytes) -> str:
    """outbound：HMAC-SHA256 over raw body（与既有 consumer.py 兼容）。"""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_webhook(secret: str, body: bytes, provided: str) -> bool:
    return hmac.compare_digest(sign_webhook(secret, body), provided)
```

- [ ] **Step 4: Run tests, verify pass**

Run: `pytest services/libs/apihub-core/tests/test_signing.py -v`
Expected: PASS 8/8

- [ ] **Step 5: Commit**

```bash
git add services/libs/apihub-core/src/apihub_core/signing.py services/libs/apihub-core/tests/test_signing.py
git commit -m "R2e T2: apihub_core.signing (inbound canonical + outbound webhook 纯函数)"
```

---

## Task 3: Migration `14-hmac-secret.sql`

**Files:**
- Create: `scripts/init-db/14-hmac-secret.sql`

**Interfaces:**
- Produces: `api_key.hmac_secret_encrypted text` (nullable), `webhook_subscription.secret_encrypted text` (nullable)

> 幂等，沿用 R2b/R3a 风格。只 ADD 列（明文 `secret` 的 scrub 走 Task 9 回填 .py，不在 SQL DROP，避免 ordering hazard）。

- [ ] **Step 1: Write migration**

```sql
-- R2e: HMAC 签名密钥列
-- api_key.hmac_secret_encrypted: opt-in 签名模式（NULL=仅 bearer）
ALTER TABLE api_key ADD COLUMN IF NOT EXISTS hmac_secret_encrypted text;
COMMENT ON COLUMN api_key.hmac_secret_encrypted IS
  'AESGCM-encrypted HMAC signing secret (b64). NULL = key not enrolled for HMAC signing.';

-- webhook_subscription.secret_encrypted: outbound webhook secret 加密存
-- （存量明文 secret 由 14-backfill-webhook-secret.py 加密回填 + SET secret=NULL scrub）
ALTER TABLE webhook_subscription ADD COLUMN IF NOT EXISTS secret_encrypted text;
COMMENT ON COLUMN webhook_subscription.secret_encrypted IS
  'AESGCM-encrypted outbound webhook signing secret (b64). NULL = no signing.';

-- RLS 已在 01-schema/06-notification ENABLE+FORCE，新列自动受既有 policy 保护，无需新 policy。
-- GRANT：apihub_app 已有 api_key/webhook_subscription 的 SELECT/INSERT/UPDATE，新列自动覆盖。
```

- [ ] **Step 2: Verify idempotency (dev stack)**

Run: `make dev-up && make db-apply && make db-apply`（二次 apply 须 no-op，因 `ADD COLUMN IF NOT EXISTS`）
Expected: 第二次 apply 无 error、无重复列。

- [ ] **Step 3: Verify columns exist**

Run: `docker exec -it $(docker ps -qf name=postgres) psql -U apihub -d apihub -c "\d api_key" | grep hmac_secret_encrypted`
Expected: 列存在，nullable。

- [ ] **Step 4: Commit**

```bash
git add scripts/init-db/14-hmac-secret.sql
git commit -m "R2e T3: migration 14 — api_key.hmac_secret_encrypted + webhook_subscription.secret_encrypted"
```

---

## Task 4: identity cache 扩展（`hmac_enrolled` + secret 缓存）

**Files:**
- Modify: `services/libs/apihub-core/src/apihub_core/identity.py`
- Test: `services/libs/apihub-core/tests/test_identity_hmac.py`

**Interfaces:**
- Consumes: `apihub_core.redis.raw_client()`
- Produces:
  - identity entry 增字段 `hmac_enrolled: bool`（write_identity 调用方填，identity 本身不改 write_identity 签名——dict 任意字段）
  - `identity.hmac_secret_cache_key(api_key: str) -> str`
  - `identity.write_hmac_secret(api_key: str, secret_encrypted: str, ttl: int) -> None`
  - `identity.read_hmac_secret(api_key: str) -> str | None`（返回**加密 blob**，caller decrypt）
  - `identity.delete_hmac_secret(api_key: str) -> None`

- [ ] **Step 1: Write failing test** — `services/libs/apihub-core/tests/test_identity_hmac.py`

```python
"""identity 缓存 hmac_enrolled + secret 缓存单测。"""

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("HMAC_SECRET_KEY", "a" * 64)
    monkeypatch.setenv("PG_HOST", "localhost")
    monkeypatch.setenv("PG_USER", "apihub")
    monkeypatch.setenv("PG_PASSWORD", "t")
    monkeypatch.setenv("REDIS_HOST", "localhost")
    from apihub_core.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def fake_redis(monkeypatch):
    import fakeredis.aioredis
    from apihub_core import redis as redis_mod
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_mod, "_client", fake)
    return fake


async def test_write_read_hmac_secret_roundtrip(fake_redis):
    from apihub_core import identity, crypto
    enc = crypto.encrypt_secret("plaintext_secret")
    await identity.write_hmac_secret("ak_xxx", enc, ttl=300)
    got = await identity.read_hmac_secret("ak_xxx")
    assert got == enc  # 返回加密 blob，不 decrypt
    assert crypto.decrypt_secret(got) == "plaintext_secret"


async def test_read_hmac_secret_miss(fake_redis):
    from apihub_core import identity
    assert await identity.read_hmac_secret("ak_missing") is None


async def test_delete_hmac_secret(fake_redis):
    from apihub_core import identity, crypto
    await identity.write_hmac_secret("ak_xxx", crypto.encrypt_secret("s"), ttl=300)
    await identity.delete_hmac_secret("ak_xxx")
    assert await identity.read_hmac_secret("ak_xxx") is None


async def test_delete_secret_does_not_clear_identity(fake_redis):
    """rotate 只清 secret 缓存，不清 identity。"""
    from apihub_core import identity
    await identity.write_identity("ak_xxx", {"tenant_id": "t1", "hmac_enrolled": True}, ttl=300)
    await identity.write_hmac_secret("ak_xxx", "encblob", ttl=300)
    await identity.delete_hmac_secret("ak_xxx")
    idc = await identity.read_identity("ak_xxx")
    assert idc is not None and idc["tenant_id"] == "t1"  # identity 仍在
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest services/libs/apihub-core/tests/test_identity_hmac.py -v`
Expected: FAIL — `AttributeError: module 'apihub_core.identity' has no attribute 'write_hmac_secret'`

- [ ] **Step 3: Implement** — append to `identity.py`:

```python
def hmac_secret_cache_key(api_key: str) -> str:
    """hmac_secret:{sha256(api_key)} —— 与 identity 分键，便于 rotate 只清 secret。"""
    return "hmac_secret:" + hashlib.sha256(api_key.encode("utf-8")).hexdigest()


async def write_hmac_secret(api_key: str, secret_encrypted: str, ttl: int) -> None:
    """写加密 secret blob（不存明文）。caller = auth create/rotate。"""
    await redis.raw_client().setex(hmac_secret_cache_key(api_key), ttl, secret_encrypted)


async def read_hmac_secret(api_key: str) -> str | None:
    """读加密 secret blob（miss/损坏返 None）。caller = dispatcher，须自行 decrypt。"""
    raw = await redis.raw_client().get(hmac_secret_cache_key(api_key))
    return raw if isinstance(raw, str) else None


async def delete_hmac_secret(api_key: str) -> None:
    await redis.raw_client().delete(hmac_secret_cache_key(api_key))
```

- [ ] **Step 4: Run tests, verify pass**

Run: `pytest services/libs/apihub-core/tests/test_identity_hmac.py -v`
Expected: PASS 4/4

- [ ] **Step 5: Commit**

```bash
git add services/libs/apihub-core/src/apihub_core/identity.py services/libs/apihub-core/tests/test_identity_hmac.py
git commit -m "R2e T4: identity 缓存 +hmac_enrolled 字段 + secret 分键缓存"
```

---

## Task 5: auth repository — secret 生命周期

**Files:**
- Modify: `services/services/auth/src/auth/repository.py`（`create_api_key` + 2 新函数）
- Test: `services/services/auth/tests/test_hmac_repo.py`

**Interfaces:**
- Consumes: `apihub_core.crypto.encrypt_secret`, `apihub_core.db`
- Produces:
  - `create_api_key(..., signing: bool=False)` → 返回 dict 增 `hmac_secret: str | None`（明文，仅创建返一次）
  - `get_hmac_secret_plaintext(key_id: str) -> str | None`（admin_db_session，跨租户；未 enrolled 返 None）
  - `rotate_hmac_secret(key_id: str) -> dict`（返 `{"key_id", "key_hash", "hmac_secret"}` 明文一次；写 audit_log）

> DB-touching 测试 stub PG（conftest 模式）；真 PG 走 kind e2e（Task 12）。`rotate_hmac_secret` 须 `RETURNING key_hash`，供 routes 按 `hmac_secret:{key_hash}` 失效缓存（key_hash = sha256(明文 api_key)，与 `identity.hmac_secret_cache_key` 一致）。

- [ ] **Step 1: Write failing test** — `services/services/auth/tests/test_hmac_repo.py`

```python
"""auth repository HMAC secret 生命周期单测（stub PG）。"""

from contextlib import contextmanager

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("HMAC_SECRET_KEY", "a" * 64)
    monkeypatch.setenv("PG_HOST", "localhost")
    monkeypatch.setenv("PG_USER", "apihub")
    monkeypatch.setenv("PG_PASSWORD", "t")
    monkeypatch.setenv("REDIS_HOST", "localhost")
    from apihub_core.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_create_api_key_signing_true_returns_secret(monkeypatch):
    from auth import repository
    from apihub_core import crypto

    captured = {}

    class _Conn:
        async def fetchrow(self, sql, *args):
            return {"id": "app_x", "tenant_id": "tenant_a"}

        async def execute(self, sql, *args):
            captured["insert_args"] = args

    @contextmanager
    def _db_session():
        yield _Conn()

    monkeypatch.setattr(repository.db, "db_session", _db_session)
    rec = await repository.create_api_key(
        key_id="key_1", app_id="app_x", tenant_id="tenant_a",
        name="n", key_hash="h", display_prefix="ak_xxxxxxxx",
        scopes=[], expires_at=None, signing=True,
    )
    assert rec["hmac_secret"] is not None and len(rec["hmac_secret"]) >= 32
    # INSERT 第 9 个 arg = hmac_secret_encrypted（加密 blob，非明文）
    assert captured["insert_args"][7] != rec["hmac_secret"]
    assert crypto.decrypt_secret(captured["insert_args"][7]) == rec["hmac_secret"]


async def test_create_api_key_signing_false_no_secret(monkeypatch):
    from auth import repository

    class _Conn:
        async def fetchrow(self, sql, *args):
            return {"id": "app_x", "tenant_id": "tenant_a"}

        async def execute(self, sql, *args):
            pass

    @contextmanager
    def _db_session():
        yield _Conn()

    monkeypatch.setattr(repository.db, "db_session", _db_session)
    rec = await repository.create_api_key(
        key_id="key_1", app_id="app_x", tenant_id="tenant_a",
        name="n", key_hash="h", display_prefix="ak_xxxxxxxx",
        scopes=[], expires_at=None, signing=False,
    )
    assert rec["hmac_secret"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest services/services/auth/tests/test_hmac_repo.py -v`
Expected: FAIL — `create_api_key` 不接 `signing` 参数

- [ ] **Step 3: Modify `create_api_key`** — add `signing` param + `hmac_secret_encrypted` column. Add top import `from apihub_core import crypto as crypto_mod`:

```python
async def create_api_key(
    *,
    key_id: str,
    app_id: str,
    tenant_id: str,
    name: str,
    key_hash: str,
    display_prefix: str,
    scopes: list[str],
    expires_at: datetime | None,
    signing: bool = False,
) -> dict:
    """插入新 APIKey（同租户 RLS 校验）。

    signing=True：额外生成 hmac_secret（明文仅返回一次），DB 存 AESGCM 加密列。
    """
    import secrets

    hmac_plaintext: str | None = None
    hmac_encrypted: str | None = None
    if signing:
        hmac_plaintext = secrets.token_urlsafe(32)
        hmac_encrypted = crypto_mod.encrypt_secret(hmac_plaintext)

    async with db.db_session() as conn:
        app = await conn.fetchrow("SELECT id, tenant_id FROM app WHERE id = $1", app_id)
        if not app:
            raise ApiError(ErrorCode.NOT_FOUND, f"app {app_id} not found in your tenant")

        await conn.execute(
            """
            INSERT INTO api_key (
                id, tenant_id, app_id, key_prefix, key_hash,
                name, scopes, status, expires_at, created_at, hmac_secret_encrypted
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, 'active', $8, NOW(), $9)
            """,
            key_id, tenant_id, app_id, display_prefix, key_hash,
            name, scopes, expires_at, hmac_encrypted,
        )

        return {
            "id": key_id,
            "app_id": app_id,
            "name": name,
            "scopes": scopes,
            "display_prefix": display_prefix,
            "expires_at": expires_at,
            "created_at": datetime.now(UTC).isoformat(),
            "hmac_secret": hmac_plaintext,
        }
```

Add `get_hmac_secret_plaintext` + `rotate_hmac_secret` after `revoke_api_key`:

```python
async def get_hmac_secret_plaintext(key_id: str) -> str | None:
    """跨租户取 key 的 HMAC secret 明文（admin_db_session，bypass RLS）。

    dispatcher 冷路径调用。未 enrolled（列 NULL）或 key 非 active → None。
    """
    async with db.admin_db_session(audit_reason="cross-tenant hmac-secret fetch") as conn:
        row = await conn.fetchrow(
            "SELECT hmac_secret_encrypted FROM api_key WHERE id = $1 AND status = 'active'",
            key_id,
        )
    if not row or not row["hmac_secret_encrypted"]:
        return None
    return crypto_mod.decrypt_secret(row["hmac_secret_encrypted"])


async def rotate_hmac_secret(key_id: str) -> dict:
    """轮换 HMAC secret → 新明文（返一次）+ RETURNING key_hash 供 caller 失效缓存。

    audit_log 由 admin_db_session 写（audit_reason=hmac_secret_rotation）。
    """
    import secrets

    new_plaintext = secrets.token_urlsafe(32)
    new_encrypted = crypto_mod.encrypt_secret(new_plaintext)
    async with db.admin_db_session(audit_reason="hmac_secret_rotation") as conn:
        row = await conn.fetchrow(
            """
            UPDATE api_key SET hmac_secret_encrypted = $2
            WHERE id = $1 AND status = 'active' AND hmac_secret_encrypted IS NOT NULL
            RETURNING id, key_hash
            """,
            key_id, new_encrypted,
        )
    if not row:
        raise ApiError(ErrorCode.NOT_FOUND, f"active enrolled api_key {key_id} not found")
    return {"key_id": row["id"], "key_hash": row["key_hash"], "hmac_secret": new_plaintext}
```

- [ ] **Step 4: Run tests, verify pass**

Run: `pytest services/services/auth/tests/test_hmac_repo.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add services/services/auth/src/auth/repository.py services/services/auth/tests/test_hmac_repo.py
git commit -m "R2e T5: auth repository — create_api_key(signing=) + get/rotate hmac_secret"
```

---

## Task 6: auth routes — create-with-signing / rotate / `/v1/internal/hmac-secret`

**Files:**
- Modify: `services/services/auth/src/auth/models.py`
- Modify: `services/services/auth/src/auth/routes.py`（create_key + rotate + internal + identity warmup 带 `key_id`/`hmac_enrolled`）
- Modify: `services/services/auth/src/auth/main.py`（skip_auth_paths）
- Test: `services/services/auth/tests/test_hmac_routes.py`

**Interfaces:**
- Consumes: `auth.repository.create_api_key(signing=)`/`rotate_hmac_secret`/`get_hmac_secret_plaintext`; `apihub_core.identity.write_identity/write_hmac_secret`; `apihub_core.crypto.encrypt_secret`
- Produces:
  - `POST /v1/apps/{app_id}/api-keys` body `signing: bool=false` → response `hmac_secret: str | None`
  - `POST /v1/api-keys/{key_id}/hmac-secret/rotate` → `{"key_id","hmac_secret"}`
  - `POST /v1/internal/hmac-secret` body `{key_id}` → `{"hmac_secret": str | null}`（admin-scoped, skip_auth_paths）

- [ ] **Step 1: Write failing test** — `services/services/auth/tests/test_hmac_routes.py`

```python
"""auth HMAC routes 单测（monkeypatch repository + identity）。"""

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("HMAC_SECRET_KEY", "a" * 64)
    monkeypatch.setenv("PG_HOST", "localhost")
    monkeypatch.setenv("PG_USER", "apihub")
    monkeypatch.setenv("PG_PASSWORD", "t")
    monkeypatch.setenv("REDIS_HOST", "localhost")
    from apihub_core.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _coro(v):
    async def _c():
        return v
    return await _c()


@pytest.fixture
def admin_client(monkeypatch):
    """平台 admin caller，bypass auth middleware。"""
    import httpx
    from apihub_core import auth as auth_mw
    from apihub_core.tenant import TenantContext
    monkeypatch.setattr(auth_mw, "authenticate_request", lambda *a, **kw: TenantContext(
        tenant_id="t_admin", tenant_type="system", is_platform_admin=True))
    from auth.main import app
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://t")


async def test_create_key_with_signing_returns_hmac_secret(admin_client, monkeypatch):
    from auth import repository
    monkeypatch.setattr(repository, "create_api_key", lambda **kw: _coro({
        "id": "key_1", "app_id": kw["app_id"], "name": kw["name"],
        "scopes": kw["scopes"], "display_prefix": "ak_xxxxxxxx",
        "expires_at": None, "created_at": "2026-07-21T00:00:00",
        "hmac_secret": "generated_secret_value",
    }))
    monkeypatch.setattr(repository, "_inject_home_region_on_create", lambda **kw: _coro(None))
    import apihub_core.identity as ident
    monkeypatch.setattr(ident, "write_identity", lambda *a, **kw: _coro(None))
    monkeypatch.setattr(ident, "write_hmac_secret", lambda *a, **kw: _coro(None))

    r = await admin_client.post("/v1/apps/app_x/api-keys", json={"name": "n", "signing": True})
    assert r.status_code == 200
    assert r.json()["hmac_secret"] == "generated_secret_value"


async def test_rotate_endpoint_returns_new_secret(admin_client, monkeypatch):
    from auth import repository
    monkeypatch.setattr(repository, "rotate_hmac_secret", lambda key_id: _coro(
        {"key_id": key_id, "key_hash": "h", "hmac_secret": "new_secret"}))
    import apihub_core.identity as ident
    monkeypatch.setattr(ident, "delete_hmac_secret", lambda *a, **kw: _coro(None))

    r = await admin_client.post("/v1/api-keys/key_1/hmac-secret/rotate")
    assert r.status_code == 200
    assert r.json()["hmac_secret"] == "new_secret"


async def test_internal_hmac_secret_cold_path(admin_client, monkeypatch):
    from auth import repository
    monkeypatch.setattr(repository, "get_hmac_secret_plaintext", lambda key_id: _coro("cold_secret"))
    r = await admin_client.post("/v1/internal/hmac-secret", json={"key_id": "key_1"})
    assert r.status_code == 200
    assert r.json()["hmac_secret"] == "cold_secret"


async def test_internal_hmac_secret_unenrolled_returns_null(admin_client, monkeypatch):
    from auth import repository
    monkeypatch.setattr(repository, "get_hmac_secret_plaintext", lambda key_id: _coro(None))
    r = await admin_client.post("/v1/internal/hmac-secret", json={"key_id": "key_1"})
    assert r.status_code == 200
    assert r.json()["hmac_secret"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest services/services/auth/tests/test_hmac_routes.py -v`
Expected: FAIL — routes 不存在 / `signing` 字段未定义

- [ ] **Step 3: Modify `models.py`** — add fields + 2 models:

```python
class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=2, max_length=64)
    scopes: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None
    signing: bool = False  # True = 该 key 走 HMAC 签名模式


class ApiKeyResponse(BaseModel):
    id: str
    app_id: str
    name: str
    scopes: list[str]
    api_key: str
    display_prefix: str
    expires_at: datetime | None = None
    created_at: datetime
    hmac_secret: str | None = None  # 明文，仅创建时返回（signing=True）


class HmacSecretRequest(BaseModel):
    """dispatcher 冷路径取 secret。"""
    key_id: str = Field(min_length=5)


class HmacSecretResponse(BaseModel):
    hmac_secret: str | None  # None = key 未 enrolled
```

- [ ] **Step 4: Modify `routes.py` create_key** — pass `signing`, warm identity with `key_id`+`hmac_enrolled`, warm secret cache:

Update imports block to add `HmacSecretRequest, HmacSecretResponse` and `rotate_hmac_secret, get_hmac_secret_plaintext` to the `from auth.repository import (...)`.

```python
    record = await create_api_key(
        key_id=key_id,
        app_id=app_id,
        tenant_id=ctx.tenant_id,
        name=payload.name,
        key_hash=key_hash,
        display_prefix=display_prefix,
        scopes=payload.scopes,
        expires_at=payload.expires_at,
        signing=payload.signing,
    )
```

In the `try:` warmup block, extend the identity payload with `key_id` + `hmac_enrolled`, and warm secret cache when enrolled:

```python
        try:
            from apihub_core import identity
            from auth.apikey import POSITIVE_CACHE_TTL

            await _inject_home_region_on_create(
                key_id=key_id, key=plaintext, tenant_id=ctx.tenant_id
            )
            identity_payload = {
                "is_active": True,
                "tenant_id": ctx.tenant_id,
                "tenant_type": ctx.tenant_type,
                "app_id": app_id,
                "is_platform_admin": ctx.is_platform_admin,
                "scopes": payload.scopes,
                "expires_at": payload.expires_at.isoformat() if payload.expires_at else None,
                "key_id": key_id,             # R2e: cold path /v1/internal/hmac-secret 入参
                "hmac_enrolled": payload.signing,
            }
            await identity.write_identity(plaintext, identity_payload, ttl=POSITIVE_CACHE_TTL)
            if payload.signing and record.get("hmac_secret"):
                from apihub_core.crypto import encrypt_secret
                await identity.write_hmac_secret(
                    plaintext, encrypt_secret(record["hmac_secret"]), ttl=POSITIVE_CACHE_TTL
                )
        except Exception:  # noqa: BLE001
            log.warning("apisix_consumer_upsert_failed", key_id=key_id, app_id=app_id, exc_info=True)
```

Add rotate + internal endpoints after `revoke_key`:

```python
    @app.post("/v1/api-keys/{key_id}/hmac-secret/rotate")
    async def rotate_hmac(key_id: str):
        """轮换 HMAC secret → 新明文仅返回一次 + 失效 secret Redis 缓存（identity 不动）。"""
        ctx = require_tenant()
        from auth.repository import rotate_hmac_secret
        from apihub_core import redis

        result = await rotate_hmac_secret(key_id)
        # 失效 warm secret 缓存：key_hash = sha256(明文 api_key)，与 hmac_secret_cache_key 一致
        await redis.raw_client().delete("hmac_secret:" + result["key_hash"])
        log.info("hmac_secret_rotated", key_id=key_id, tenant_id=ctx.tenant_id)
        # 不回传 key_hash（内部用）
        return {"key_id": result["key_id"], "hmac_secret": result["hmac_secret"]}

    @app.post("/v1/internal/hmac-secret", response_model=HmacSecretResponse)
    async def fetch_hmac_secret(payload: HmacSecretRequest):
        """dispatcher 冷路径取 HMAC secret 明文（集群内 + admin_db_session bypass RLS）。

        等价 /v1/apikey/verify 的冷回源。未 enrolled 返 hmac_secret=None。
        """
        from auth.repository import get_hmac_secret_plaintext
        secret = await get_hmac_secret_plaintext(payload.key_id)
        return HmacSecretResponse(hmac_secret=secret)
```

- [ ] **Step 5: Modify `main.py`** — add to `skip_auth_paths`:

```python
        "/v1/internal/hmac-secret",  # dispatcher 冷路径取 HMAC secret（集群内 NetworkPolicy）
```

- [ ] **Step 6: Run tests, verify pass**

Run: `pytest services/services/auth/tests/test_hmac_routes.py -v`
Expected: PASS 4/4

- [ ] **Step 7: Commit**

```bash
git add services/services/auth/src/auth/models.py services/services/auth/src/auth/routes.py services/services/auth/src/auth/main.py services/services/auth/tests/test_hmac_routes.py
git commit -m "R2e T6: auth routes — create-with-signing + rotate + /v1/internal/hmac-secret"
```

---

## Task 7: `apihub_core/auth.py: _verify_hmac`（inbound 验签编排）

**Files:**
- Modify: `services/libs/apihub-core/src/apihub_core/auth.py`
- Modify: `services/libs/apihub-core/src/apihub_core/middleware.py:80-84`（`X-App-Key` 抽取）
- Test: `services/libs/apihub-core/tests/test_hmac_verify.py`

> **Pre-flight fix:** middleware 当前只抽 `X-API-Key`/`Authorization`（middleware.py:81），HMAC 请求（`X-App-Key` only）会在 `if not api_key` 处被拒。本任务须让 middleware 也抽 `X-App-Key`，并让 HMAC 检测门只看 `X-App-Key` 存在（不看 `X-Signature`）——否则 enrolled key 不带签名会落 bearer 路径绕过（违反 §6 "enrolled 必须签名"）。bearer 用 `X-API-Key`/`Authorization`，与 `X-App-Key` 无冲突。

**Interfaces:**
- Consumes: `apihub_core.signing.verify`, `apihub_core.identity.read_identity/read_hmac_secret`, `apihub_core.crypto.decrypt_secret/encrypt_secret`, `apihub_core.config.Settings`, `apihub_core.redis.raw_client`
- Produces: `authenticate_request` 在检测到 HMAC 头时分流到 `_verify_hmac`，返回 `TenantContext`

- [ ] **Step 1: Write failing test** — `services/libs/apihub-core/tests/test_hmac_verify.py`

```python
"""inbound HMAC 验签编排单测（mock secret 源 + fake redis）。"""

import time

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("HMAC_SECRET_KEY", "a" * 64)
    monkeypatch.setenv("PG_HOST", "localhost")
    monkeypatch.setenv("PG_USER", "apihub")
    monkeypatch.setenv("PG_PASSWORD", "t")
    monkeypatch.setenv("REDIS_HOST", "localhost")
    from apihub_core.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def fake_redis(monkeypatch):
    import fakeredis.aioredis
    from apihub_core import redis as redis_mod
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_mod, "_client", fake)
    return fake


def _make_request(*, method="POST", path="/v1/foo", query="", body=b'{"x":1}',
                  app_key="ak_enrolledkey", secret="the_secret", timestamp=None, nonce="n1"):
    from apihub_core.signing import sign
    ts = timestamp or str(int(time.time()))
    sig = sign(secret, method, path + (("?" + query) if query else ""), body, ts)

    class _Req:
        pass
    req = _Req()
    req.headers = {
        "X-App-Key": app_key,
        "X-Signature": sig,
        "X-Timestamp": ts,
        "X-Nonce": nonce,
    }
    req.method = method
    req.url = type("U", (), {"path": path, "query": query})()
    _body_bytes = body
    async def _body():
        return _body_bytes
    req.body = _body
    return req


async def _seed_enrolled(fake_redis, secret="the_secret"):
    from apihub_core import identity, crypto
    await identity.write_identity("ak_enrolledkey", {
        "is_active": True, "tenant_id": "t1", "tenant_type": "internal",
        "app_id": "app1", "key_id": "key_1", "hmac_enrolled": True,
    }, ttl=300)
    await identity.write_hmac_secret("ak_enrolledkey", crypto.encrypt_secret(secret), ttl=300)


async def test_enrolled_correct_signature_passes(fake_redis):
    from apihub_core.auth import authenticate_request
    from apihub_core.config import get_settings
    await _seed_enrolled(fake_redis)
    req = _make_request()
    ctx = await authenticate_request(req, get_settings(), api_key="ak_enrolledkey")
    assert ctx.tenant_id == "t1"


async def test_unenrolled_key_with_signature_rejected(fake_redis):
    from apihub_core import identity
    from apihub_core.auth import authenticate_request
    from apihub_core.config import get_settings
    from apihub_core.errors import ApiError
    await identity.write_identity("ak_plain", {
        "is_active": True, "tenant_id": "t1", "tenant_type": "internal",
        "app_id": "app1", "key_id": "key_2", "hmac_enrolled": False,
    }, ttl=300)
    req = _make_request(app_key="ak_plain", secret="x")
    with pytest.raises(ApiError, match="not enrolled"):
        await authenticate_request(req, get_settings(), api_key="ak_plain")


async def test_enrolled_key_missing_signature_rejected(fake_redis):
    """enrolled key 不带签名头 → 401（防降级 bearer 绕过）。"""
    from apihub_core.auth import authenticate_request
    from apihub_core.config import get_settings
    from apihub_core.errors import ApiError
    await _seed_enrolled(fake_redis)
    class _Req: pass
    req = _Req()
    req.headers = {"X-App-Key": "ak_enrolledkey"}  # 无 X-Signature
    req.method = "POST"
    req.url = type("U", (), {"path": "/v1/foo", "query": ""})()
    async def _b(): return b'{}'
    req.body = _b
    with pytest.raises(ApiError, match="hmac signing required"):
        await authenticate_request(req, get_settings(), api_key="ak_enrolledkey")


async def test_tampered_body_rejected(fake_redis):
    from apihub_core.auth import authenticate_request
    from apihub_core.config import get_settings
    from apihub_core.errors import ApiError
    from apihub_core.signing import sign
    await _seed_enrolled(fake_redis)
    # 签 {"x":1} 的 canonical，但 body 是 {"x":2}
    req = _make_request(body=b'{"x":2}')
    req.headers["X-Signature"] = sign("the_secret", "POST", "/v1/foo", b'{"x":1}', req.headers["X-Timestamp"])
    with pytest.raises(ApiError, match="invalid signature"):
        await authenticate_request(req, get_settings(), api_key="ak_enrolledkey")


async def test_replay_nonce_rejected(fake_redis):
    from apihub_core.auth import authenticate_request
    from apihub_core.config import get_settings
    from apihub_core.errors import ApiError
    await _seed_enrolled(fake_redis)
    req1 = _make_request(nonce="dup")
    await authenticate_request(req1, get_settings(), api_key="ak_enrolledkey")
    req2 = _make_request(nonce="dup")
    with pytest.raises(ApiError, match="replay"):
        await authenticate_request(req2, get_settings(), api_key="ak_enrolledkey")


async def test_stale_timestamp_rejected(fake_redis):
    from apihub_core.auth import authenticate_request
    from apihub_core.config import get_settings
    from apihub_core.errors import ApiError
    await _seed_enrolled(fake_redis)
    old_ts = str(int(time.time()) - 600)  # -10min，超 ±5min 窗
    req = _make_request(timestamp=old_ts)
    with pytest.raises(ApiError, match="timestamp"):
        await authenticate_request(req, get_settings(), api_key="ak_enrolledkey")


async def test_corrupt_secret_cache_returns_503(fake_redis):
    """secret 缓存密文损坏（decrypt 抛 InvalidTag）→ 503 + DEL 缓存（不当 401）。"""
    from apihub_core import identity
    from apihub_core.auth import authenticate_request
    from apihub_core.config import get_settings
    from apihub_core.errors import ApiError
    await _seed_enrolled(fake_redis)
    # 用坏 blob 覆盖
    await identity.write_hmac_secret("ak_enrolledkey", "!!!not-valid-b64!!!", ttl=300)
    req = _make_request()
    with pytest.raises(ApiError) as exc_info:
        await authenticate_request(req, get_settings(), api_key="ak_enrolledkey")
    assert exc_info.value.http_status == 503
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest services/libs/apihub-core/tests/test_hmac_verify.py -v`
Expected: FAIL — `_verify_hmac` 不存在 / 无 HMAC 分流

- [ ] **Step 3: Implement** — add HMAC detection in `authenticate_request` + `_verify_hmac`.

In `authenticate_request`, after the `if not api_key:` guard, before the `X-Ingress-Auth` block, add:

```python
    # HMAC 签名分流（R2e）：带 X-App-Key 的请求走 in-app 验签（bearer 用 X-API-Key/Authorization，无冲突）。
    # 只看 X-App-Key 存在（不看 X-Signature）——enrolled key 不带签名也要进 _verify_hmac 拒掉（防降级绕过）。
    # JWT 流优先（eyJ 开头不进 HMAC 分流）。
    from apihub_core import jwt_utils  # 既有 lazy import
    if not jwt_utils.is_jwt(api_key) and request.headers.get("X-App-Key"):
        return await _verify_hmac(request, settings, api_key)
```

(Note: `jwt_utils` is imported lazily later in the function; move/import it here or reuse. Ensure no double-import error — keep as-is, the lazy `from apihub_core import jwt_utils` already exists below; re-import is idempotent.)

Also update **middleware.py:80-84** to extract `X-App-Key` so HMAC requests carry the credential past the `if not api_key` guard:

```python
        # 从 header 取 X-API-Key 或 Authorization: Bearer 或 X-App-Key（HMAC 签名流，R2e）
        api_key = (
            request.headers.get("X-API-Key")
            or _extract_bearer(request.headers.get("Authorization"))
            or request.headers.get("X-App-Key")
        )
```

Implement `_verify_hmac`:

```python
async def _verify_hmac(request: Request, settings: Settings, api_key: str) -> TenantContext:
    """in-app HMAC 验签（R2e）。

    1. identity 缓存取 ctx + hmac_enrolled
    2. enrolled 校验（未 enrolled 带签名头 → 401；enrolled 不带 → 401）
    3. 取 secret：warm=Redis 加密 blob+decrypt；cold=auth /v1/internal/hmac-secret
    4. timestamp ±window；nonce SETNX TTL；verify compare_digest
    """
    from apihub_core import crypto as crypto_mod, identity, signing
    from apihub_core.errors import ApiError, ErrorCode
    from apihub_core.redis import raw_client

    cached = await identity.read_identity(api_key)
    if cached is None or cached.get("invalid"):
        raise ApiError(ErrorCode.UNAUTHORIZED, "invalid api key")
    if not cached.get("is_active"):
        raise ApiError(ErrorCode.UNAUTHORIZED, "Invalid API Key")

    enrolled = cached.get("hmac_enrolled", False)
    has_sig = bool(request.headers.get("X-Signature"))
    if enrolled and not has_sig:
        raise ApiError(ErrorCode.UNAUTHORIZED, "hmac signing required for this key")
    if not enrolled and has_sig:
        raise ApiError(ErrorCode.UNAUTHORIZED, "key not enrolled for hmac")

    # 取 secret
    secret_blob = await identity.read_hmac_secret(api_key)
    if secret_blob is not None:
        try:
            secret = crypto_mod.decrypt_secret(secret_blob)
        except Exception:  # InvalidTag —— 非客户端错，503 + DEL 缓存
            await raw_client().delete(identity.hmac_secret_cache_key(api_key))
            raise ApiError(ErrorCode.INTERNAL, "hmac secret cache corrupt", http_status=503)
    else:
        key_id = cached.get("key_id")
        if not key_id:
            raise ApiError(ErrorCode.INTERNAL, "hmac secret fetch: missing key_id", http_status=503)
        async with httpx.AsyncClient(timeout=5.0) as client:
            try:
                resp = await client.post(
                    settings.hmac_secret_service_url,
                    json={"key_id": key_id},
                    headers={"X-Internal-Service": settings.app_name},
                )
            except httpx.RequestError as e:
                raise ApiError(
                    ErrorCode.INTERNAL,
                    f"auth unreachable: {type(e).__name__}: {e!r}",
                    http_status=503,
                ) from e
        if resp.status_code != 200:
            raise ApiError(ErrorCode.UNAUTHORIZED, "hmac secret fetch failed")
        secret = resp.json().get("hmac_secret")
        if not secret:
            raise ApiError(ErrorCode.UNAUTHORIZED, "key not enrolled for hmac")
        await identity.write_hmac_secret(
            api_key, crypto_mod.encrypt_secret(secret), ttl=settings.hmac_nonce_ttl_seconds
        )

    # timestamp ±window
    ts_raw = request.headers.get("X-Timestamp", "")
    try:
        ts = int(ts_raw)
    except ValueError:
        raise ApiError(ErrorCode.UNAUTHORIZED, "invalid timestamp")
    if abs(int(time.time()) - ts) > settings.hmac_timestamp_window_seconds:
        raise ApiError(ErrorCode.UNAUTHORIZED, "stale timestamp")

    # nonce SETNX
    nonce = request.headers.get("X-Nonce", "")
    if not nonce:
        raise ApiError(ErrorCode.UNAUTHORIZED, "invalid nonce")
    nonce_key = f"t:{cached['tenant_id']}:hmac:nonce:{api_key}:{nonce}"
    set_ok = await raw_client().set(nonce_key, "1", ex=settings.hmac_nonce_ttl_seconds, nx=True)
    if not set_ok:
        raise ApiError(ErrorCode.UNAUTHORIZED, "replay detected")

    # verify
    body = await request.body()
    raw_path = request.url.path + (("?" + request.url.query) if request.url.query else "")
    if not signing.verify(secret, request.method, raw_path, body, ts_raw, request.headers.get("X-Signature", "")):
        raise ApiError(ErrorCode.UNAUTHORIZED, "invalid signature")

    ctx = TenantContext(
        tenant_id=cached["tenant_id"],
        tenant_type=cached.get("tenant_type", "internal"),
        app_id=cached.get("app_id"),
        is_platform_admin=cached.get("is_platform_admin", False),
    )
    set_tenant_context(ctx)
    return ctx
```

Add `import time` at top of `auth.py` if not present.

- [ ] **Step 4: Run tests, verify pass**

Run: `pytest services/libs/apihub-core/tests/test_hmac_verify.py -v`
Expected: PASS 7/7

- [ ] **Step 5: Regression — bearer/JWT path untouched**

Run: `pytest services/libs/apihub-core/tests/ services/services/auth/tests/ -v`
Expected: no new failures vs R3c baseline (apihub-core 121/0/15-skip, auth 81/0)

- [ ] **Step 6: Commit**

```bash
git add services/libs/apihub-core/src/apihub_core/auth.py services/libs/apihub-core/tests/test_hmac_verify.py
git commit -m "R2e T7: apihub_core.auth _verify_hmac (inbound 验签编排 + 挂入 authenticate_request)"
```

---

## Task 8: outbound webhook signer（notification）

**Files:**
- Modify: `services/services/notification/src/notification/consumer.py`
- Modify: `services/services/notification/src/notification/repository.py`
- Modify: `services/services/notification/src/notification/models.py`
- Test: `services/services/notification/tests/test_outbound_signing.py`

**Interfaces:**
- Consumes: `apihub_core.signing.sign_webhook`, `apihub_core.crypto.encrypt_secret/decrypt_secret`
- Produces: `_deliver` 发 `X-Webhook-Signature: hmac-sha256=<hex>`；`create_webhook` 平台生成 secret 返明文一次

- [ ] **Step 1: Write failing test** — `services/services/notification/tests/test_outbound_signing.py`

```python
"""outbound webhook 签名单测。"""

from contextlib import contextmanager

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("HMAC_SECRET_KEY", "a" * 64)
    monkeypatch.setenv("PG_HOST", "localhost")
    monkeypatch.setenv("PG_USER", "apihub")
    monkeypatch.setenv("PG_PASSWORD", "t")
    monkeypatch.setenv("REDIS_HOST", "localhost")
    from apihub_core.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_deliver_sets_webhook_signature_header(monkeypatch):
    from notification import consumer
    import apihub_core.signing as signing

    captured = {}

    class _Resp:
        status_code = 200

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, content=None, headers=None):
            captured["body"] = content
            captured["sig"] = headers.get("X-Webhook-Signature", "")
            return _Resp()

    monkeypatch.setattr(consumer.httpx, "AsyncClient", lambda *a, **kw: _Client())
    ok = await consumer._deliver("http://x", {"e": 1}, "wh_secret")
    assert ok is True
    sig_hex = captured["sig"].removeprefix("hmac-sha256=")
    assert signing.verify_webhook("wh_secret", captured["body"], sig_hex) is True


async def test_deliver_no_secret_no_signature(monkeypatch):
    from notification import consumer
    captured = {}

    class _Resp:
        status_code = 200

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, content=None, headers=None):
            captured["sig"] = headers.get("X-Webhook-Signature", "")
            return _Resp()

    monkeypatch.setattr(consumer.httpx, "AsyncClient", lambda *a, **kw: _Client())
    await consumer._deliver("http://x", {"e": 1}, "")
    assert captured["sig"] == ""


async def test_create_webhook_generates_secret(monkeypatch):
    from notification import repository
    from apihub_core import crypto

    captured = {}

    class _Conn:
        async def execute(self, sql, *args):
            captured["args"] = args

    @contextmanager
    def _db():
        yield _Conn()

    monkeypatch.setattr(repository.db, "db_session", _db)
    result = await repository.create_webhook(tenant_id="t1", url="http://x", events=["api.call.*"], secret=None)
    assert result["hmac_secret"] is not None
    assert crypto.decrypt_secret(captured["args"][4]) == result["hmac_secret"]


async def test_create_webhook_client_supplied_secret_compatible(monkeypatch):
    from notification import repository
    from apihub_core import crypto
    captured = {}

    class _Conn:
        async def execute(self, sql, *args):
            captured["args"] = args

    @contextmanager
    def _db():
        yield _Conn()

    monkeypatch.setattr(repository.db, "db_session", _db)
    result = await repository.create_webhook(tenant_id="t1", url="http://x", events=["api.call.*"], secret="my_secret")
    assert result["hmac_secret"] == "my_secret"
    assert crypto.decrypt_secret(captured["args"][4]) == "my_secret"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest services/services/notification/tests/test_outbound_signing.py -v`
Expected: FAIL — 头格式不对 / `create_webhook` 不返 hmac_secret

- [ ] **Step 3: Modify `consumer.py`** — `_deliver` + `_get_active_webhooks`:

```python
from apihub_core.signing import sign_webhook
from apihub_core.crypto import decrypt_secret


async def _deliver(url: str, payload: dict, secret: str) -> bool:
    """推送到 Webhook URL（带 HMAC-SHA256 over raw body）。secret 空 → 不签名（向后兼容）。"""
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-Webhook-Signature"] = f"hmac-sha256={sign_webhook(secret, body)}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(url, content=body, headers=headers)
            return r.status_code < 500
    except httpx.RequestError:
        return False


async def _get_active_webhooks() -> list[dict]:
    """取所有 active webhook（含解密 secret）。"""
    async with db.admin_db_session() as conn:
        rows = await conn.fetch(
            "SELECT id, tenant_id, url, events, secret_encrypted FROM webhook_subscription WHERE status = 'active'"
        )
    out = []
    for r in rows:
        d = dict(r)
        enc = d.pop("secret_encrypted", None)
        d["secret"] = decrypt_secret(enc) if enc else ""
        out.append(d)
    return out
```

(Remove the now-unused `import hmac`/`import hashlib` from consumer.py top if ruff flags — they may still be used elsewhere; leave if so.)

- [ ] **Step 4: Modify `repository.py:create_webhook`**:

```python
async def create_webhook(*, tenant_id: str, url: str, events: list[str], secret: str | None) -> dict:
    """创建 webhook。secret=None → 平台生成（返明文一次，DB 存加密）；client-supplied → 加密存同值。"""
    from apihub_core.crypto import encrypt_secret
    import secrets
    wh_id = f"wh_{secrets.token_hex(8)}"
    plaintext = secret if secret else secrets.token_urlsafe(32)
    encrypted = encrypt_secret(plaintext)
    async with db.db_session() as conn:
        await conn.execute(
            "INSERT INTO webhook_subscription (id, tenant_id, url, events, secret_encrypted)"
            " VALUES ($1, $2, $3, $4, $5)",
            wh_id, tenant_id, url, events, encrypted,
        )
    return {"id": wh_id, "url": url, "events": events, "status": "active", "hmac_secret": plaintext}
```

- [ ] **Step 5: Modify `models.py`** — `WebhookResponse` add `hmac_secret`:

```python
class WebhookResponse(BaseModel):
    id: str
    url: str
    events: list[str]
    status: str
    created_at: str
    hmac_secret: str | None = None  # 明文，仅创建返一次
```

- [ ] **Step 6: Run tests, verify pass**

Run: `pytest services/services/notification/tests/test_outbound_signing.py -v`
Expected: PASS 4/4

- [ ] **Step 7: Commit**

```bash
git add services/services/notification/src/notification/consumer.py services/services/notification/src/notification/repository.py services/services/notification/src/notification/models.py services/services/notification/tests/test_outbound_signing.py
git commit -m "R2e T8: outbound webhook — sign_webhook + X-Webhook-Signature + 平台生成 secret 加密存"
```

---

## Task 9: backfill script `14-backfill-webhook-secret.py`

**Files:**
- Create: `scripts/init-db/14-backfill-webhook-secret.py`

- [ ] **Step 1: Write script**

```python
#!/usr/bin/env python3
"""R2e 回填：存量 webhook_subscription.secret(明文) → secret_encrypted(AESGCM) + scrub 明文。

幂等：二次跑无 secret IS NOT NULL 行即 no-op。
apply 顺序：先 apply 14-hmac-secret.sql（ADD secret_encrypted）→ 跑本脚本 →
secret 列保留 NULL 占位（不 DROP，见 spec §4.2 rationale）。
"""
import asyncio
import sys

import asyncpg

from apihub_core.crypto import encrypt_secret


async def main(pg_dsn: str) -> None:
    conn = await asyncpg.connect(pg_dsn)
    try:
        rows = await conn.fetch(
            "SELECT id, secret FROM webhook_subscription "
            "WHERE secret IS NOT NULL AND secret_encrypted IS NULL"
        )
        if not rows:
            print("backfill: no rows; no-op")
            return
        for r in rows:
            enc = encrypt_secret(r["secret"])
            await conn.execute(
                "UPDATE webhook_subscription SET secret_encrypted=$1, secret=NULL WHERE id=$2",
                enc, r["id"],
            )
        print(f"backfill: encrypted + scrubbed {len(rows)} webhook secrets")
    finally:
        await conn.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: 14-backfill-webhook-secret.py <pg_dsn>", file=sys.stderr)
        sys.exit(2)
    asyncio.run(main(sys.argv[1]))
```

- [ ] **Step 2: Smoke test (dev/kind)** — with a seeded plaintext-secret webhook row:

Run: `python scripts/init-db/14-backfill-webhook-secret.py "postgresql://apihub:apihub_dev_pwd@localhost:5432/apihub"`
Expected: `backfill: encrypted + scrubbed N webhook secrets` or `no-op`.

Verify: `SELECT id, secret, secret_encrypted FROM webhook_subscription` → `secret` NULL, `secret_encrypted` non-null.

- [ ] **Step 3: Idempotency** — run twice; second `no-op`.

- [ ] **Step 4: Commit**

```bash
git add scripts/init-db/14-backfill-webhook-secret.py
git commit -m "R2e T9: backfill script — webhook 明文 secret 加密回填 + scrub"
```

---

## Task 10: docs `docs/hmac-signing.md`

**Files:**
- Create: `docs/hmac-signing.md`

- [ ] **Step 1: Write doc** — full prose (no placeholders). Sections:
  1. Inbound canonical 串定义 + query 保留规则
  2. Headers: `X-App-Key`/`X-Timestamp`/`X-Signature`/`X-Nonce`
  3. Python 示例（`from apihub_core.signing import sign`）
  4. curl 示例（`openssl dgst -sha256 -hmac`）
  5. Outbound webhook：`X-Webhook-Signature: hmac-sha256=<hex>` over raw body，`verify_webhook`
  6. Rotation：`POST /v1/api-keys/{key_id}/hmac-secret/rotate`
  7. fail-closed / replay / timestamp 窗语义

- [ ] **Step 2: Commit**

```bash
git add docs/hmac-signing.md
git commit -m "R2e T10: docs/hmac-signing — canonical + headers + Python/curl 示例"
```

---

## Task 11: deploy env（auth + dispatcher + notification）

**Files:**
- Modify: `deploy/k8s/services/auth/deployment.yaml`
- Modify: `deploy/k8s/services/dispatcher/deployment.yaml`
- Modify: `deploy/k8s/services/notification/deployment.yaml`

- [ ] **Step 1: Inspect pattern** — `grep -n "AI_GATEWAY_ENCRYPTION_KEY\|envFrom\|secretKeyRef" deploy/k8s/services/ai-gateway/deployment.yaml`

- [ ] **Step 2: Add env** to the three deployments, mirroring ai-gateway's approach for `AI_GATEWAY_ENCRYPTION_KEY`:

```yaml
        - name: HMAC_SECRET_KEY
          valueFrom:
            secretKeyRef:
              name: apihub-hmac-key
              key: hmac_secret_key
```

(dev may use `value:` from configmap if ai-gateway does; prod must use secretKeyRef.)

- [ ] **Step 3: Commit**

```bash
git add deploy/k8s/services/auth/deployment.yaml deploy/k8s/services/dispatcher/deployment.yaml deploy/k8s/services/notification/deployment.yaml
git commit -m "R2e T11: deploy — auth/dispatcher/notification 注入 HMAC_SECRET_KEY"
```

---

## Task 12: kind e2e + 全量回归

**Files:** 无（验证轮）

- [ ] **Step 1: apply schema + backfill**

Run: `make db-apply`（applies 14-hmac-secret.sql）
Run: `python scripts/init-db/14-backfill-webhook-secret.py <kind_pg_dsn>`（no-op if无 seeded webhooks）
Expected: schema applied, columns exist.

- [ ] **Step 2: deploy services with HMAC_SECRET_KEY**

Run: `make k8s-apply-dev`
Expected: auth/dispatcher/notification pods 1/1 Running（fail-closed 通过——env 存在）。

- [ ] **Step 3: inbound HMAC e2e**

- Create HMAC key: `POST /v1/apps/{app_id}/api-keys {"name":"hmac-test","signing":true}` → 捕 `api_key` + `hmac_secret`。
- 签名请求 → APISIX → dispatcher → 200。
- 篡改 body → 401 `invalid signature`。
- replay nonce → 401。
- stale timestamp → 401。
- rotate → old sig 401, new sig 200。
- bearer key（非 signing）请求 → 200（回归）。

- [ ] **Step 4: outbound webhook e2e**

- 创建 webhook subscription → 捕 `hmac_secret`。
- 触发 api-call-event → notification consumer POST 带 `X-Webhook-Signature: hmac-sha256=<hex>`。
- mock server `verify_webhook` 通过。

- [ ] **Step 5: RLS check**

- 跨租户 `get_hmac_secret_plaintext` 非 admin → RLS 拒（无行）。

- [ ] **Step 6: Full regression**

Run: `pytest services/ -v`
Expected: apihub-core（R3c 基线 121/0/15-skip）+ auth 81/0 + notification（R2b 基线）+ trace——无新失败。bearer/JWT 零回归。

- [ ] **Step 7: lint/typecheck**

Run: `make lint && make fmt`
Expected: ruff clean, mypy clean.

- [ ] **Step 8: final opus whole-branch review**

Per user working style（spec→plan→handoff，one squash-PR per round）：派 opus whole-branch review；处理 Critical/Important；handoff 用户 push/merge。

---

## Self-Review (写 plan 后自查)

**Spec coverage:**
- §1 背景/决策 → Global Constraints + Architecture ✓
- §2 架构（inbound/outbound/canonical） → Task 2+7+8 ✓
- §3 组件 C1-C5 → Task 1(C1)+2(C2)+7(C3)+5/6(C4)+8(C5) ✓
- §4 schema/migration → Task 3+9 ✓
- §4.5 env → Task 1+11 ✓
- §4.7 部署 gate → Task 11+12 ✓
- §5 测试 T1-T8 → 各 Task 单测 + Task 12 e2e ✓
- §6 错误处理 → Task 7 `_verify_hmac` 各分支 + Task 8 outbound NULL ✓
- §7 客户端 helper/doc → Task 2 + Task 10 ✓
- §8 defer → 不在本轮 ✓

**Type consistency:**
- `encrypt_secret`/`decrypt_secret` — Task 1 定义，Task 4/5/7/8/9 调用一致 ✓
- `signing.sign`/`verify`/`sign_webhook`/`verify_webhook` — Task 2 定义，Task 7/8 调用一致 ✓
- `identity.write_hmac_secret`/`read_hmac_secret`/`delete_hmac_secret`/`hmac_secret_cache_key` — Task 4 定义，Task 6/7 调用一致 ✓
- `get_hmac_secret_plaintext`/`rotate_hmac_secret`（返 `key_hash`） — Task 5 定义，Task 6 调用一致 ✓
- `identity` payload 带 `key_id`+`hmac_enrolled` — Task 6 写，Task 7 读 ✓
- `rotate` 失效缓存用 `redis.raw_client().delete("hmac_secret:" + key_hash)` — Task 5 RETURNING key_hash，Task 6 调用，`hmac_secret_cache_key` = `"hmac_secret:" + sha256(明文)` = `key_hash` ✓

**已 resolve 的 spec 歧义：**
- C2 outbound "复用 sign()" → 拆 `sign_webhook`/`verify_webhook`（body-only），与既有 consumer.py 逐字节兼容（Task 2 test_webhook_matches_raw_hmac 守护）。
- rotate 失效缓存无明文 → `rotate_hmac_secret` RETURNING `key_hash`，按 `hmac_secret:{key_hash}` 失效（Task 5+6）。
