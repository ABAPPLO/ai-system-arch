"""字段脱敏 —— 给日志副本用，不影响返回给调用方的真实响应。

action:
  - remove   完全移除字段
  - mask     部分显示（手机号 138****1234、邮箱 x***@xxx.com）
  - hash     SHA256 hash

支持点路径：`user.phone`、`users[].phone`（数组里每个对象的 phone）。
"""

import hashlib
import re
from copy import deepcopy
from typing import Any


def mask_value(value: Any, action: str) -> Any:
    if value is None:
        return None
    if action == "remove":
        return None
    if action == "hash":
        return hashlib.sha256(str(value).encode()).hexdigest()[:16]
    if action == "mask":
        return _smart_mask(str(value))
    return value


def _smart_mask(s: str) -> str:
    """根据内容自动选脱敏策略：
    手机号  → 138****1234
    邮箱    → x***@xxx.com
    身份证  → 110101********1234
    其他    → 长度<4 全 *，长则保留首尾各 1
    """
    if re.fullmatch(r"1[3-9]\d{9}", s):
        return f"{s[:3]}****{s[-4:]}"
    if "@" in s and s.count("@") == 1:
        local, domain = s.split("@")
        return f"{local[0]}***@{domain}"
    if re.fullmatch(r"\d{15}|\d{17}[\dXx]", s):  # 身份证
        return f"{s[:6]}********{s[-4:]}"
    if len(s) <= 4:
        return "*" * len(s)
    return f"{s[0]}{'*' * (len(s) - 2)}{s[-1]}"


def apply_masking(data: Any, rules: list[dict] | None) -> Any:
    """根据 rules 列表脱敏 data（深拷贝后修改）。

    rules 形如：
        [{"field": "phone", "action": "mask"},
         {"field": "user.email", "action": "mask"},
         {"field": "users[].id_card", "action": "hash"},
         {"field": "password", "action": "remove"}]
    """
    if not rules or data is None:
        return data
    result = deepcopy(data)
    for rule in rules:
        field = rule.get("field")
        action = rule.get("action", "mask")
        if not field:
            continue
        _apply_one(result, field, action)
    return result


def _apply_one(data: Any, field_path: str, action: str) -> None:
    """按 'a.b.c' 或 'a[].b' 路径定位字段并脱敏。"""
    parts = field_path.split(".")
    _walk(data, parts, 0, action)


def _walk(node: Any, parts: list[str], idx: int, action: str) -> None:
    if idx >= len(parts):
        return

    part = parts[idx]
    is_last = idx == len(parts) - 1

    # 数组通配 users[].phone
    if part.endswith("[]"):
        key = part[:-2]
        if not isinstance(node, dict) or key not in node:
            return
        arr = node[key]
        if not isinstance(arr, list):
            return
        if is_last:
            # 整个数组脱敏（少见）
            if action == "remove":
                node.pop(key, None)
            else:
                node[key] = [mask_value(x, action) for x in arr]
        else:
            for item in arr:
                if isinstance(item, dict):
                    _walk(item, parts, idx + 1, action)
        return

    if not isinstance(node, dict):
        return

    if is_last:
        if action == "remove":
            node.pop(part, None)
        elif part in node:
            node[part] = mask_value(node[part], action)
        return

    if part in node:
        _walk(node[part], parts, idx + 1, action)
