"""auth —— APIKey 鉴权服务。

职责：
1. APIKey 生成（ak_ 前缀 + 32 字随机）
2. APIKey 校验（SHA256 hash 匹配 + Redis 缓存 + 状态检查）
3. APIKey 吊销（标记 status=revoked + 清缓存）
4. 应用密钥列表 / 元数据查询

详见 docs/03-services.md §3.3 + docs/08-observability-security.md §7
"""
