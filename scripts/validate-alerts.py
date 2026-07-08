#!/usr/bin/env python3
"""校验 Prometheus 告警规则文件。

CI 集成用：
    python scripts/validate-alerts.py

校验项：
  1. YAML 语法
  2. 每条 rule 必填字段：alert, expr, for, labels.severity
  3. severity 必须在 {P0, P1, P2, P3}
  4. labels.team 必须存在（路由依赖）
  5. annotations.summary 必须存在（钉钉模板依赖）
  6. 不得有重名 alert（跨文件也不行）
  7. expr 非空字符串

失败返回非 0。
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("PyYAML not installed: pip install pyyaml", file=sys.stderr)
    sys.exit(1)


VALID_SEVERITIES = {"P0", "P1", "P2", "P3"}
REQUIRED_LABELS = {"severity", "team"}
REQUIRED_ANNOTATIONS = {"summary"}


def validate_rule(rule: dict, file: Path, seen_names: set[str]) -> list[str]:
    errors: list[str] = []
    name = rule.get("alert")

    if not name:
        errors.append(f"{file}: rule without 'alert' name")
        return errors

    if name in seen_names:
        errors.append(f"{file}: duplicate alert name '{name}'")
    seen_names.add(name)

    # expr
    expr = rule.get("expr")
    if not expr:
        errors.append(f"{file}: {name} missing 'expr'")
    elif not isinstance(expr, str) or not expr.strip():
        errors.append(f"{file}: {name} empty 'expr'")

    # for
    if "for" not in rule:
        errors.append(f"{file}: {name} missing 'for'")

    # labels
    labels = rule.get("labels") or {}
    for required in REQUIRED_LABELS:
        if required not in labels:
            errors.append(f"{file}: {name} missing labels.{required}")

    sev = labels.get("severity")
    if sev and sev not in VALID_SEVERITIES:
        errors.append(
            f"{file}: {name} invalid severity={sev!r}, must be {sorted(VALID_SEVERITIES)}"
        )

    # annotations
    annotations = rule.get("annotations") or {}
    for required in REQUIRED_ANNOTATIONS:
        if required not in annotations:
            errors.append(f"{file}: {name} missing annotations.{required}")

    return errors


def validate_file(path: Path, seen_names: set[str]) -> tuple[list[str], int]:
    """返回 (errors, rule_count)。"""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        return [f"{path}: YAML parse error: {e}"], 0
    except FileNotFoundError:
        return [f"{path}: file not found"], 0

    if not isinstance(data, dict):
        return [f"{path}: top-level must be a mapping"], 0

    groups = data.get("groups") or []
    if not isinstance(groups, list):
        return [f"{path}: 'groups' must be a list"], 0

    all_errors: list[str] = []
    rule_count = 0
    for grp in groups:
        if not isinstance(grp, dict):
            all_errors.append(f"{path}: group must be a mapping")
            continue
        gname = grp.get("name", "<unnamed>")
        rules = grp.get("rules") or []
        for r in rules:
            if not isinstance(r, dict):
                all_errors.append(f"{path}: group {gname} rule must be a mapping")
                continue
            all_errors.extend(validate_rule(r, path, seen_names))
            rule_count += 1

    return all_errors, rule_count


def main() -> int:
    rules_dir = Path("scripts/prometheus/rules")
    if not rules_dir.exists():
        print(f"{rules_dir}: directory not found", file=sys.stderr)
        return 1

    yaml_files = sorted([*rules_dir.glob("*.yml"), *rules_dir.glob("*.yaml")])
    if not yaml_files:
        print(f"⚠️  {rules_dir} has no *.yml files", file=sys.stderr)
        return 0

    all_errors: list[str] = []
    total_rules = 0
    seen_names: set[str] = set()
    for f in yaml_files:
        errs, count = validate_file(f, seen_names)
        all_errors.extend(errs)
        total_rules += count
        if not errs:
            severity_breakdown = {}
            data = yaml.safe_load(f.read_text(encoding="utf-8"))
            for grp in data.get("groups") or []:
                for r in grp.get("rules") or []:
                    sev = (r.get("labels") or {}).get("severity", "?")
                    severity_breakdown[sev] = severity_breakdown.get(sev, 0) + 1
            breakdown = ", ".join(
                f"{k}={v}" for k, v in sorted(severity_breakdown.items())
            )
            print(f"✅ {f.name}: {count} rules ({breakdown})")

    print(f"\n✨ {total_rules} rules across {len(yaml_files)} files")

    if all_errors:
        print("\n❌ Validation errors:", file=sys.stderr)
        for e in all_errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
