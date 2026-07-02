#!/usr/bin/env python3
"""校验 schema/ 目录下所有 YAML 接口定义。

CI 集成用：
    python scripts/validate-schema.py

失败返回非 0。
"""

import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("PyYAML not installed: pip install pyyaml", file=sys.stderr)
    sys.exit(1)


REQUIRED_FIELDS = {
    "api": ["name", "category", "description", "base_path"],
    "version": ["version", "backend_type", "backend_url", "method", "path"],
}

VALID_BACKEND_TYPES = {"http", "async_task", "workflow", "ai_model"}
VALID_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH"}


def validate_file(path: Path) -> list[str]:
    errors: list[str] = []

    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        return [f"{path}: YAML parse error: {e}"]

    if not isinstance(data, dict):
        return [f"{path}: top-level must be a mapping"]

    for section, fields in REQUIRED_FIELDS.items():
        if section not in data:
            errors.append(f"{path}: missing section '{section}'")
            continue
        for f in fields:
            if f not in data[section]:
                errors.append(f"{path}: {section}.{f} required")

    version = data.get("version", {})
    bt = version.get("backend_type")
    if bt and bt not in VALID_BACKEND_TYPES:
        errors.append(
            f"{path}: invalid backend_type '{bt}', must be {VALID_BACKEND_TYPES}"
        )

    method = version.get("method")
    if method and method not in VALID_METHODS:
        errors.append(f"{path}: invalid method '{method}'")

    # AI 模型校验
    if bt == "ai_model":
        if not version.get("ai_model"):
            errors.append(f"{path}: ai_model requires 'ai_model' field")
        if "billing" not in version:
            errors.append(f"{path}: ai_model recommends 'billing' section")

    return errors


def main() -> int:
    schema_dir = Path("schema")
    if not schema_dir.exists():
        print("schema/ directory not found", file=sys.stderr)
        return 1

    yaml_files = list(schema_dir.rglob("*.yaml")) + list(schema_dir.rglob("*.yml"))
    if not yaml_files:
        print("⚠️  schema/ has no YAML files", file=sys.stderr)
        return 0

    all_errors: list[str] = []
    for f in sorted(yaml_files):
        errs = validate_file(f)
        if errs:
            all_errors.extend(errs)
        else:
            print(f"✅ {f}")

    if all_errors:
        print("\n❌ Validation errors:", file=sys.stderr)
        for e in all_errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    print(f"\n✨ {len(yaml_files)} schemas passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
