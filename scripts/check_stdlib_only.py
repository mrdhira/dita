#!/usr/bin/env python3
"""Fail if any module under the given roots imports something outside the standard library.

The DIP packages are specified to have zero third-party dependencies, including whatever a
code generator might have decided to emit. This is what checks that, rather than trusting it.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

LOCAL_ROOTS = {"dip"}


def imported_modules(source: str) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


def main(roots: list[str]) -> int:
    offenders: dict[str, set[str]] = {}
    scanned = 0

    for root in roots:
        for path in sorted(Path(root).rglob("*.py")):
            scanned += 1
            third = {
                module
                for module in imported_modules(path.read_text(encoding="utf-8"))
                if module not in sys.stdlib_module_names and module not in LOCAL_ROOTS
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

    print(f"  stdlib only ({scanned} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["."]))
