"""apihub-apply CLI 入口。

子命令：
  validate <path>           仅解析校验，不调远端
  apply <path> --env dev    推到 api-registry
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable
from pathlib import Path

from apihub_cli.apply import ApplyResult, apply_definition
from apihub_cli.client import RegistryClient
from apihub_cli.models import (
    ApiDefinition,
    load_dir,
    load_yaml,
)


def _iter_definitions(path: Path) -> Iterable[ApiDefinition]:
    if path.is_dir():
        yield from load_dir(path)
    elif path.is_file() and path.suffix in (".yaml", ".yml"):
        yield load_yaml(path)
    else:
        raise FileNotFoundError(f"{path} is not a yaml file or directory")


def cmd_validate(args: argparse.Namespace) -> int:
    path = Path(args.path)
    try:
        defs = list(_iter_definitions(path))
    except Exception as e:  # noqa: BLE001
        print(f"❌ {e}", file=sys.stderr)
        return 1

    print(f"✅ {len(defs)} definition(s) parsed:")
    for d in defs:
        print(
            f"  - {d.api.name} @ {d.api.base_path} "
            f"({d.version.backend_type.value} {d.version.method.value} {d.version.path})"
        )
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    path = Path(args.path)

    try:
        defs = list(_iter_definitions(path))
    except Exception as e:  # noqa: BLE001
        print(f"❌ {e}", file=sys.stderr)
        return 1

    if not defs:
        print("⚠️  no YAML files to apply", file=sys.stderr)
        return 0

    print(f"Applying {len(defs)} definition(s) to env={args.env}...")

    with RegistryClient(
        base_url=args.base_url, api_key=args.api_key,
    ) as client:
        results: list[ApplyResult] = []
        for d in defs:
            if args.dry_run:
                print(f"  [DRY-RUN] would apply: {d.api.name}")
                continue
            r = apply_definition(
                d,
                client=client,
                target_env=args.env,
                submitted_by=args.submitted_by,
            )
            results.append(r)
            _print_result(r)

    if args.dry_run:
        return 0

    failed = [r for r in results if not r.is_ok()]
    if failed:
        print(f"\n❌ {len(failed)}/{len(results)} failed", file=sys.stderr)
        return 1
    print(f"\n✨ {len(results)} applied")
    return 0


def _print_result(r: ApplyResult) -> None:
    if not r.is_ok():
        print(f"  ❌ {r.api_name}: {r.error}")
        return
    status_marker = "✅" if r.change_request_status == "approved" else "⏳"
    print(
        f"  {status_marker} {r.api_name}: "
        f"api={r.api_id} version={r.version_id} "
        f"cr#{r.change_request_id} ({r.change_request_status})"
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="apihub-apply")
    sub = p.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate", help="仅校验 YAML，不调远端")
    v.add_argument("path", type=str)
    v.set_defaults(func=cmd_validate)

    a = sub.add_parser("apply", help="推到 api-registry")
    a.add_argument("path", type=str)
    a.add_argument("--base-url", default="http://localhost:8000")
    a.add_argument("--api-key", default="dev_local", help="X-API-Key")
    a.add_argument("--env", default="dev", choices=["dev", "staging", "prod"])
    a.add_argument("--submitted-by", default="ci@apihub")
    a.add_argument("--dry-run", action="store_true")
    a.set_defaults(func=cmd_apply)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
