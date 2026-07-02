"""APIKey 生成 / 哈希 / 前缀 —— 纯函数，无副作用。

格式规范（docs/08-observability-security.md §7.2）：
  - 前缀 ak_ 明文展示（易于日志检索）
  - 长度 32 字符的 URL-safe 随机串
  - 数据库只存 SHA256 hash
  - 明文仅在创建时返回一次
"""

import hashlib
import secrets

# 全局常量
KEY_PREFIX = "ak_"
RANDOM_LENGTH = 32  # token_urlsafe(32) → ~43 字符 base64
DISPLAY_PREFIX_LEN = 8  # 前 8 位明文展示（搜索用）

NEGATIVE_CACHE_TTL = 60  # 非法 key 负缓存 1 分钟（防爆破）
POSITIVE_CACHE_TTL = 300  # 合法 key 正缓存 5 分钟


def generate_api_key() -> tuple[str, str, str]:
    """生成一对 (plaintext, hash, prefix_for_display)。

    Returns:
        plaintext: 完整明文（仅返回给调用方一次）
        key_hash: SHA256 hash（存数据库）
        display_prefix: 前 8 位（用于列表展示）
    """
    raw = secrets.token_urlsafe(RANDOM_LENGTH)
    plaintext = f"{KEY_PREFIX}{raw}"
    return plaintext, hash_api_key(plaintext), plaintext[:DISPLAY_PREFIX_LEN]


def hash_api_key(plaintext: str) -> str:
    """SHA256 hash —— 数据库唯一索引。"""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def cache_key(plaintext_or_hash: str) -> str:
    """构造 Redis 缓存 key。

    可以传明文或 hash（统一用 hash 做缓存 key，避免明文进 Redis）。
    """
    if plaintext_or_hash.startswith(KEY_PREFIX):
        return f"ak:{hash_api_key(plaintext_or_hash)}"
    return f"ak:{plaintext_or_hash}"


def is_valid_format(plaintext: str) -> bool:
    """快速格式校验 —— 防止明显垃圾请求打到 DB。

    不查 DB 就能拒绝 90% 的扫描攻击。
    """
    if not plaintext or not plaintext.startswith(KEY_PREFIX):
        return False
    return len(plaintext) >= DISPLAY_PREFIX_LEN + 8
