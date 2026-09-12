#!/usr/bin/env python3
"""Fail if any module under the given roots imports something outside the standard library.

The DIP packages are specified to have zero third-party dependencies, including whatever a
code generator might have decided to emit. This is what checks that, rather than trusting it.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

# Sibling workspace packages are not third-party. Anything else a package genuinely needs
# is declared with --allow, so it is visible in the Makefile rather than hidden in here.
LOCAL_ROOTS = {"dip", "dita_worker", "ocr_worker"}


def imported_modules(source: str) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


def main(argv: list[str]) -> int:
    allowed = set()
    roots = []
    pending_allow = False
    for arg in argv:
        if pending_allow:
            allowed |= {name.strip() for name in arg.split(",") if name.strip()}
            pending_allow = False
        elif arg == "--allow":
            pending_allow = True
        else:
            roots.append(arg)

    offenders: dict[str, set[str]] = {}
    scanned = 0

    for root in roots or ["."]:
        for path in sorted(Path(root).rglob("*.py")):
            scanned += 1
            third = {
                module
                for module in imported_modules(path.read_text(encoding="utf-8"))
                if module not in sys.stdlib_module_names
                and module not in LOCAL_ROOTS
                and module not in allowed
            }
            if third:
                offenders[str(path)] = third

    if not scanned:
        print(f"  no python files under {', '.join(roots)}", file=sys.stderr)
        return 1
    if offenders:
        for path, modules in offenders.items():
            print(f"  THIRD-PARTY: {path} imports {', '.join(sorted(modules))}", file=sys.stderr)
        return 1

    note = f" plus {', '.join(sorted(allowed))}" if allowed else ""
    print(f"  stdlib only{note} ({scanned} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
