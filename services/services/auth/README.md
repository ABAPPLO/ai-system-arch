# auth

> APIKey 鉴权服务 —— 生成 / 校验 / 吊销 + Redis 热点缓存。
> 详见 [docs/03-services.md §3.3](../../../docs/03-services.md) + [docs/08-observability-security.md §7](../../../docs/08-observability-security.md)。

## 接口

| 方法 | 路径 | 说明 | 鉴权 |
|------|------|------|------|
| POST | `/v1/apikey/verify`             | **内部**：dispatcher 等通过它校验 APIKey | skip（K8s NetworkPolicy 限制集群内） |
| POST | `/v1/apps/{app_id}/api-keys`    | 为 app 创建新 APIKey（明文仅此一次返回） | APIKey |
| GET  | `/v1/apps/{app_id}/api-keys`    | 列出 app 的所有 key（不含明文） | APIKey |
| DELETE | `/v1/api-keys/{key_id}`       | 吊销 key（同租户 RLS + 清缓存） | APIKey |
| GET  | `/v1/auth/health`               | 自身健康 | skip |
| GET  | `/health/live` / `/health/ready` | k8s probe | skip |

## 校验流程（`/v1/apikey/verify`）

```
incoming api_key
   ↓
1. is_valid_format?         否 → 401（不打 DB 不打 Redis）
   ↓ 是
2. Redis GET ak:{sha256}    命中 → 区分正/负缓存
   ↓ miss
3. PG 跨租户查（admin_db_session）
   SET LOCAL app.is_platform_admin = 'true'
   SELECT ak.*, tenant.* WHERE key_hash = $1
   ↓ 找到
4. 检查 status / expires_at
   ↓ OK
5. 写 Redis 正缓存（5min）+ 异步更新 last_used_at
   ↓ 找不到
6. 写 Redis 负缓存（1min）—— 防爆破
```

## 关键设计

### 1. 缓存策略

| 类型 | TTL | 目的 |
|------|-----|------|
| 正缓存（合法） | 5 min | 让 dispatcher 几乎每次命中缓存，不打 PG |
| 负缓存（非法） | 1 min | 防爆破扫描同一个垃圾 key 反复打 DB |
| 吊销 | 主动 DEL | 不能等 TTL 过期 |

缓存 key：`ak:{sha256(plaintext)}`，**明文不入 Redis**。

### 2. 跨租户查询（admin_db_session）

verify 是入口端点：调用方还没 tenant context，必须跨租户查 `api_key` 表。专用 `admin_db_session()` 设 `is_platform_admin=true` 绕过 RLS。

⚠️ 该 session 仅用于 auth 服务这个端点 + 平台运维。业务代码禁用。

### 3. skip_auth_paths

`/v1/apikey/verify` 是 APIKey 校验入口，不能递归依赖 APIKey 校验。靠 K8s NetworkPolicy 限制来源（只允许集群内带 `apihub.io/cluster-internal=true` 标签的 namespace 调用）。

### 4. 明文 vs hash

| 字段 | 存储 | 暴露 |
|------|------|------|
| plaintext api_key | 创建时返回一次，不存 | 创建响应 |
| key_hash (SHA256) | DB `api_key.key_hash` | 内部 |
| key_prefix (`ak_abcdef`) | DB `api_key.key_prefix` | 列表展示 |
| cache key (`ak:{sha256}`) | Redis | 内部 |

## 本地开发

```bash
make install
make dev-up              # 起 PG/Redis
make run-auth            # uvicorn auth.main:app --reload --port 8002
```

调用示例：

```bash
# 内部 verify
curl -X POST http://localhost:8002/v1/apikey/verify \
     -H "Content-Type: application/json" \
     -d '{"api_key": "ak_test_demo001234567890"}'

# 创建（需要先有合法 APIKey 鉴权）
curl -X POST http://localhost:8002/v1/apps/app_demo/api-keys \
     -H "X-API-Key: ak_existing..." \
     -H "Content-Type: application/json" \
     -d '{"name": "prod key", "scopes": ["read"]}'
```

## 测试

```bash
cd services/services/auth
pytest tests/ -v
# 43 tests, all pass
```

覆盖：
- `apikey.py`（22）—— 生成 / 哈希 / 缓存 key 构造 / 格式校验
- `cache.py`（9）—— 正负缓存读写 / invalidate / 损坏缓存自愈
- `routes.py`（12）—— verify 全分支 / create / list / revoke / health

mock 策略：DB 层（repository 函数）和 Redis 层（`_client`）都替换成 spy，HTTP 测试用 `httpx.ASGITransport` 直打 app。

## 性能预算（prod）

- 3 副本起步（auth 不是热点，靠 Redis 缓存命中率 95%+）
- 单副本 2000m CPU / 2Gi 内存
- verify P99 < 10ms（缓存命中）/ < 30ms（缓存未命中）
