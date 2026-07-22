# HMAC 请求签名（R2e）

APIHub 支持在 `api_key`（bearer）之上叠加一层 **opt-in HMAC 签名验签**：enrolled 的 key
必须对每个请求签名，平台 in-app 验签。它解决两类问题：

- **防重放 / 防篡改**：请求体、路径、方法、时间戳都进签名；带 nonce 防重放。
- **凭证不裸传 / 防降级**：enrolled key 只能走 `X-App-Key` + 签名；用 `X-API-Key`/bearer 调用同样被拒（401），泄漏的 key 无法绕过验签。

另外，平台对外推送的 **outbound webhook** 也用同一签名原语签名，客户端可验。

> 单一真相源：`services/libs/apihub-core/src/apihub_core/signing.py`。

---

## 1. Inbound —— canonical 串定义

签名覆盖一个 **canonical 串**，其定义（`signing.canonical_string`）：

```
canonical = f"{method}\n{raw_path_with_query}\n{timestamp}\n{sha256(body).hexdigest()}"
signature = HMAC-SHA256(secret, canonical).hexdigest()   # hex
```

字段：

| 字段 | 含义 | 取值规则 |
|---|---|---|
| `method` | HTTP 方法大写 | `POST` / `GET` / … |
| `raw_path_with_query` | 原始 path + query | **保持 client wire 原样**（percent-encoded，**不 normalize、不 re-encode**）。query 为空则不含 `?`。例：`/v1/foo?a=1&b=2` |
| `timestamp` | 秒级 Unix 时间戳字符串 | 与 `X-Timestamp` 头一致 |
| `sha256(body)` | 请求体的 SHA-256 hex | 空 body → `sha256(b"")` |

> **query 为什么不 normalize？** 客户端与服务端对 query 的 percent-encoding 细节（大小写、
> 保留字）可能不同；一旦服务端 re-encode，签名必然对不上。因此平台用 client 发来的原始
> wire 字节参与签名，客户端只签自己实际发出的串即可。

所有签名比对都走 `hmac.compare_digest`（常时比对，防 timing 攻击）。

---

## 2. Inbound —— 请求头

| 头 | 必填 | 说明 |
|---|---|---|
| `X-App-Key` | 是 | enrolled 的 api_key 明文（凭证本身）。注意：HMAC 流**用 `X-App-Key`**，与 bearer 的 `X-API-Key` / `Authorization: Bearer` 区分，互不冲突 |
| `X-Timestamp` | 是 | 秒级 Unix 时间戳，须在服务端时间 ±300s（`hmac_timestamp_window_seconds`）内 |
| `X-Nonce` | 是 | 每次请求唯一的随机串。服务端用 `SET NX` 记录，TTL 600s 内重复 → 拒（防重放） |
| `X-Signature` | 是 | 上节的 `signature` hex |

### 失败语义（全部 401，除缓存损坏 503）

| 情况 | 状态 | 信息 |
|---|---|---|
| enrolled key 但**不带** `X-Signature` | 401 | `hmac signing required for this key`（防降级绕过 bearer） |
| enrolled key 走 **bearer**（`X-API-Key`/`Authorization`，无 `X-App-Key`） | 401 | `hmac signing required for this key`（`verify_api_key_record` 返 `hmac_enrolled`，bearer 路径与 X-Ingress-Auth 快路径均拒） |
| 未 enrolled key 却**带**签名头 | 401 | `key not enrolled for hmac` |
| `X-Timestamp` 超出 ±300s 窗 / 非数字 | 401 | `stale timestamp` / `invalid timestamp` |
| `X-Nonce` 重复 / 缺失 | 401 | `replay detected` / `invalid nonce` |
| 签名不匹配（body/path 被篡改） | 401 | `invalid signature` |
| secret 缓存密文损坏（decrypt 失败） | **503** | `hmac secret cache corrupt`（非客户端错，清缓存并告警） |

---

## 3. Python 客户端示例

直接复用平台同款纯函数，保证字节级一致：

```python
import time, uuid, requests
from apihub_core.signing import sign  # 或把 signing.py 的 sign 复制到客户端

app_key = "ak_xxxxxxxxxxxxxxxx"   # enrolled key（创建时返回一次）
secret  = "创建 key 时返回的 hmac_secret"
body    = b'{"x":1}'
method  = "POST"
path    = "/v1/foo?a=1"
ts      = str(int(time.time()))
nonce   = uuid.uuid4().hex

sig = sign(secret, method, path, body, ts)

r = requests.post(
    f"https://apihub.example.com{path}",
    data=body,
    headers={
        "X-App-Key": app_key,
        "X-Timestamp": ts,
        "X-Nonce": nonce,
        "X-Signature": sig,
        "Content-Type": "application/json",
    },
)
```

> 注意 `data=body`（原始字节），不要用 `json=...`——后者会让库重新序列化，body 字节可能
> 与你参与签名的不一致。签的是**实际发出的字节**。

---

## 4. curl 示例

```bash
APP_KEY="ak_xxxxxxxxxxxxxxxx"
SECRET="创建 key 时返回的 hmac_secret"
METHOD="POST"
PATH="/v1/foo?a=1"
BODY='{"x":1}'

TIMESTAMP=$(date +%s)
NONCE=$(uuidgen | tr -d -)
BODY_SHA=$(printf '%s' "$BODY" | openssl dgst -sha256 | awk '{print $NF}')
# canonical = METHOD \n PATH \n TIMESTAMP \n BODY_SHA
CANONICAL=$(printf '%s\n%s\n%s\n%s' "$METHOD" "$PATH" "$TIMESTAMP" "$BODY_SHA")
SIGNATURE=$(printf '%s' "$CANONICAL" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $NF}')

curl -X POST "https://apihub.example.com${PATH}" \
  -H "X-App-Key: ${APP_KEY}" \
  -H "X-Timestamp: ${TIMESTAMP}" \
  -H "X-Nonce: ${NONCE}" \
  -H "X-Signature: ${SIGNATURE}" \
  -H "Content-Type: application/json" \
  -d "$BODY"
```

---

## 5. Outbound webhook 签名

平台向 `webhook_subscription.url` 推送事件时，对 **raw body** 做 HMAC-SHA256，头格式：

```
X-Webhook-Signature: hmac-sha256=<hex>
```

`<hex> = HMAC-SHA256(secret, raw_body).hexdigest()`（body-only canonical，不含 method/path/timestamp）。
secret 在**创建 webhook 时由平台生成**（或 client 传入），以 AES-GCM 加密存库（`secret_encrypted`），
明文仅创建响应里返回一次。

客户端验签（Python）：

```python
import hmac, hashlib
expected = hmac.new(secret.encode(), raw_body_bytes, hashlib.sha256).hexdigest()
if not hmac.compare_digest(expected, received_hex):
    raise ValueError("bad signature")
```

或复用平台函数：`apihub_core.signing.verify_webhook(secret, raw_body_bytes, received_hex)`。

> 无 secret 的旧 webhook（`secret_encrypted` 为 NULL）→ 推送**不带**签名头（向后兼容）。

---

## 6. Secret 轮换（rotation）

轮换 HMAC signing secret，新明文**仅返回一次**：

```bash
curl -X POST "https://apihub.example.com/v1/api-keys/${KEY_ID}/hmac-secret/rotate" \
  -H "X-API-Key: ${ADMIN_BEARER_KEY}"
# → {"key_id": "...", "hmac_secret": "<new plaintext, save once>"}
```

轮换语义：

- DB `api_key.hmac_secret_encrypted` 更新为新加密值（`admin_db_session`，bypass RLS + 写 audit_log）。
- 失效 Redis warm secret 缓存（`hmac_secret:{key_hash}`），下一次签名请求走 cold path
  （auth `/v1/internal/hmac-secret`）取回并回填新 secret。
- **identity 缓存不清**（tenant_id / scopes / `hmac_enrolled` 等不变）。
- 旧 secret 立即失效：用旧 secret 签的请求 → 401 `invalid signature`。

webhook secret 目前无独立 rotate 端点（重新创建 webhook 即得新 secret）。

---

## 7. 安全语义小结

- **fail-closed**：缺 `HMAC_SECRET_KEY`（envelope 加密 key）→ 服务启动期 `RuntimeError`，不进请求路径。
- **enrolled 必须签名**：enrolled key 不带 `X-Signature` → 401；且 **bearer 路径整体拒绝 enrolled key**（`X-API-Key` 调用 enrolled key → 401，X-Ingress-Auth 快路径亦拒），泄漏的 key 无法绕过验签。
- **防重放**：`X-Nonce` 经 Redis `SET NX`（TTL = `hmac_nonce_ttl_seconds` = 600s），同 nonce 只能用一次。
- **防篡改 + 防重排**：method / path+query / timestamp / body 全进 canonical。
- **时间窗**：`X-Timestamp` 须在服务端 ±300s 内，配合 nonce TTL 收口重放窗口。
- **常时比对**：所有签名/摘要比对走 `hmac.compare_digest`。
- **envelope 加密**：secret 可逆加密存 PG（验签需真实字节重算），不存单向 hash。env key
  `HMAC_SECRET_KEY` 独立于 `AI_GATEWAY_ENCRYPTION_KEY`（爆炸半径隔离）。

---

## 附：cold path（dispatcher 取 secret）

dispatcher 验签暖路径：Redis 读加密 secret blob → in-process decrypt。miss 时回源 auth：

```
POST /v1/internal/hmac-secret   {"key_id": "..."}   → {"hmac_secret": "<plaintext|null>"}
```

该端点在 `skip_auth_paths`（集群内 K8s NetworkPolicy 限制来源，同 `/v1/apikey/verify`），
`admin_db_session` 跨租户取 + bypass RLS。未 enrolled → `hmac_secret=null`（非 401，
dispatcher 据此判定该 key 不走签名模式）。
